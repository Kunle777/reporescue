import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Clock3,
  FolderGit2,
  LoaderCircle,
  Play,
  Plus,
} from "lucide-react";
import { ApiError } from "../lib/api";
import { listJobs } from "../lib/jobs";
import type { JobCard, Project } from "../lib/types";
import ProjectList from "../components/ProjectList";

function dateLabel(value: string | null) {
  if (!value) return "Time unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Time unavailable"
    : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function statusClass(status: string) {
  if (status === "completed") return "activity-status neutral";
  if (status === "failed") return "activity-status danger";
  if (status === "cancelled") return "activity-status muted";
  return "activity-status active";
}

export default function DashboardView({
  username,
  projects,
  loading,
  error,
  onRetry,
  onGoProjects,
  onGoWorkbench,
  onChanged,
  onUnauthorized,
  onOpenInvestigation,
}: {
  username: string | null;
  projects: Project[];
  loading: boolean;
  error: string;
  onRetry: () => void;
  onGoProjects: () => void;
  onGoWorkbench: () => void;
  onChanged: () => void;
  onUnauthorized: () => void;
  onOpenInvestigation: (jobId: string) => void;
}) {
  const [jobs, setJobs] = useState<JobCard[]>([]);
  const [jobsLoading, setJobsLoading] = useState(true);
  const [jobsError, setJobsError] = useState("");

  async function loadJobs() {
    setJobsLoading(true);
    setJobsError("");
    try {
      setJobs(await listJobs(5));
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 401) {
        onUnauthorized();
      } else {
        setJobsError(requestError instanceof Error ? requestError.message : "Could not load recent investigations.");
      }
    } finally {
      setJobsLoading(false);
    }
  }

  useEffect(() => {
    let live = true;
    void listJobs(5).then((nextJobs) => {
      if (live) setJobs(nextJobs);
    }).catch((requestError: unknown) => {
      if (!live) return;
      if (requestError instanceof ApiError && requestError.status === 401) onUnauthorized();
      else setJobsError(requestError instanceof Error ? requestError.message : "Could not load recent investigations.");
    }).finally(() => {
      if (live) setJobsLoading(false);
    });
    return () => { live = false; };
  }, [onUnauthorized]);

  const firstName = username || "there";

  return (
    <div className="view-stack dashboard-view">
      <header className="page-intro dashboard-intro">
        <div>
          <p className="eyebrow">YOUR ENGINEERING WORKSPACE</p>
          <h2>Welcome back, {firstName}</h2>
          <p>Keep repository context close and investigations grounded in evidence.</p>
        </div>
        <div className="page-actions">
          <button type="button" className="btn btn-secondary" onClick={onGoProjects}>
            <FolderGit2 size={16} /> Repositories
          </button>
          <button type="button" className="btn btn-primary" onClick={onGoWorkbench}>
            <Play size={16} /> New investigation
          </button>
        </div>
      </header>

      <div className="dashboard-summary" aria-live="polite">
        <div className="summary-icon"><FolderGit2 size={19} /></div>
        <div><strong>{loading ? "—" : projects.length}</strong><span>connected {projects.length === 1 ? "repository" : "repositories"}</span></div>
        <span className="summary-divider" aria-hidden="true" />
        <p>Repository registration stores the project in your workspace. Inspection pins a revision for an investigation.</p>
      </div>

      <div className="dashboard-grid">
        <section className="surface-section repository-overview" aria-labelledby="dashboard-repositories-title">
          <div className="section-title-row">
            <div>
              <p className="eyebrow">PROJECT CONTEXT</p>
              <h3 id="dashboard-repositories-title">Repositories</h3>
            </div>
            {projects.length > 0 && <button type="button" className="text-action" onClick={onGoProjects}>Manage <ArrowRight size={14} /></button>}
          </div>
          {loading ? (
            <div className="loading-block" role="status"><LoaderCircle className="spin" size={17} /> Loading repositories…</div>
          ) : error ? (
            <div className="banner banner-error" role="alert"><AlertTriangle size={17} /><div><strong>Repositories unavailable</strong><p>{error}</p></div><button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>Retry</button></div>
          ) : projects.length === 0 ? (
            <div className="dashboard-empty">
              <span className="empty-mark"><FolderGit2 size={21} /></span>
              <div><strong>Start with a repository</strong><p>Add a public GitHub repository to keep its identity ready for your next investigation.</p></div>
              <button type="button" className="btn btn-primary btn-sm" onClick={onGoProjects}><Plus size={15} /> Add repository</button>
            </div>
          ) : (
            <ProjectList projects={projects.slice(0, 5)} compact onChanged={onChanged} onUnauthorized={onUnauthorized} />
          )}
        </section>

        <section className="surface-section recent-overview" aria-labelledby="recent-investigations-title">
          <div className="section-title-row">
            <div>
              <p className="eyebrow">PERSISTED JOBS</p>
              <h3 id="recent-investigations-title">Recent investigations</h3>
            </div>
            <Clock3 className="section-icon" size={18} aria-hidden="true" />
          </div>
          {jobsLoading ? (
            <div className="loading-block" role="status"><LoaderCircle className="spin" size={17} /> Loading recent activity…</div>
          ) : jobsError ? (
            <div className="inline-error" role="alert"><p>{jobsError}</p><button type="button" className="text-action" onClick={() => void loadJobs()}>Retry <ArrowRight size={14} /></button></div>
          ) : jobs.length ? (
            <ul className="activity-list">
              {jobs.map((job) => (
                <li key={job.job_id}>
                  <button type="button" className="activity-row" onClick={() => onOpenInvestigation(job.job_id)}>
                    <span className="activity-mark"><FolderGit2 size={16} /></span>
                    <span className="activity-copy"><strong>{job.repo_name}</strong><span>{job.mode} · {dateLabel(job.updated_at)}</span></span>
                    <span className={statusClass(job.status)}>{job.status}</span>
                    <ArrowRight className="activity-arrow" size={15} aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <div className="quiet-empty"><span className="empty-mark"><Clock3 size={19} /></span><p>Your saved investigations will appear here.</p></div>
          )}
        </section>
      </div>
    </div>
  );
}
