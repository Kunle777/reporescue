import { useEffect, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { FlaskConical, FolderGit2 } from "lucide-react";
import { ApiError } from "../lib/api";
import { listJobs } from "../lib/jobs";
import type { JobCard, Project, View } from "../lib/types";

const ACTIVE = new Set(["queued", "running", "waiting"]);

function statusPill(status: string): string {
  if (ACTIVE.has(status)) return "pill blue";
  if (status === "completed") return "pill green";
  if (status === "failed") return "pill red";
  if (status === "cancelled") return "pill";
  return "pill amber";
}

export default function DashboardView({
  session,
  projects,
  onNavigate,
  onResume,
  onUnauthorized,
}: {
  session: Session | null;
  projects: Project[];
  onNavigate: (view: View) => void;
  onResume: (jobId: string) => void;
  onUnauthorized: () => void;
}) {
  const [jobs, setJobs] = useState<JobCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      if (!session) {
        setLoading(false);
        return;
      }
      try {
        const cards = await listJobs(20);
        if (!cancelled) setJobs(cards);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          onUnauthorized();
        } else if (!cancelled) {
          setError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [session, onUnauthorized]);

  const active = jobs.filter((job) => ACTIVE.has(job.status));
  const completed = jobs.filter((job) => job.status === "completed").length;
  const firstActive = active[0];

  return (
    <div>
      <div className="view-head">
        <div>
          <h2>Welcome{session?.user?.email ? `, ${session.user.email}` : ""}</h2>
          <p>Pick up where you left off, or start a new investigation.</p>
        </div>
        <button className="btn btn-primary" onClick={() => onNavigate("new-investigation")}>
          <FlaskConical size={15} /> New investigation
        </button>
      </div>

      <div className="stat-grid">
        <div className="card stat-card">
          <span className="stat-icon"><FolderGit2 size={17} /></span>
          <div><strong>{projects.length}</strong><small>Registered repositories</small></div>
        </div>
        <div className="card stat-card">
          <span className="stat-icon">◷</span>
          <div><strong>{active.length}</strong><small>Active investigations</small></div>
        </div>
        <div className="card stat-card">
          <span className="stat-icon">✓</span>
          <div><strong>{completed}</strong><small>Completed (recent jobs)</small></div>
        </div>
      </div>

      {error && <div className="banner banner-error" role="alert"><div><strong>Could not load investigations</strong><p>{error}</p></div></div>}

      {loading ? (
        <div className="card"><p className="hint">Loading investigations…</p></div>
      ) : (
        <>
          {firstActive && (
            <section className="card">
              <div className="card-head">
                <h3>Continue investigating</h3>
                <span className={statusPill(firstActive.status)}>
                  {firstActive.status.toUpperCase()} · {firstActive.progress_pct}%
                </span>
              </div>
              <div className="recent-list">
                <div className="recent-item">
                  <span className="recent-repo">{firstActive.repo_name}</span>
                  <span className="recent-meta">
                    {firstActive.mode} · attempt {firstActive.attempts}/{firstActive.max_attempts}
                  </span>
                  <button className="btn btn-primary btn-sm" onClick={() => onResume(firstActive.job_id)}>
                    Resume
                  </button>
                </div>
              </div>
            </section>
          )}

          <section className="card">
            <div className="card-head">
              <h3>Recent investigations</h3>
              <button className="btn btn-secondary btn-sm" onClick={() => onNavigate("workbench")}>
                View history
              </button>
            </div>
            {jobs.length === 0 ? (
              <div className="empty-state">
                <strong>No investigations yet</strong>
                <p>Inspect a repository and start your first investigation.</p>
              </div>
            ) : (
              <div className="recent-list">
                {jobs.slice(0, 5).map((job) => (
                  <button
                    key={job.job_id}
                    type="button"
                    className="recent-item recent-button"
                    onClick={() => onResume(job.job_id)}
                  >
                    <span className="recent-repo">{job.repo_name}</span>
                    <span className="recent-meta">
                      {job.mode} · {job.status}
                      {job.error ? ` · ${job.error.reason}` : ""}
                    </span>
                    <span className={statusPill(job.status)}>{job.status.toUpperCase()}</span>
                  </button>
                ))}
              </div>
            )}
          </section>

          <section className="card">
            <div className="card-head">
              <h3>Repositories</h3>
              <button className="btn btn-secondary btn-sm" onClick={() => onNavigate("projects")}>
                View all
              </button>
            </div>
            {projects.length === 0 ? (
              <div className="empty-state">
                <strong>No repositories registered</strong>
                <p>Add a GitHub repository to inspect it with RepoRescue.</p>
              </div>
            ) : (
              <div className="recent-list">
                {projects.slice(0, 5).map((project) => (
                  <div key={project.id} className="recent-item">
                    <span className="recent-repo">{project.display_name}</span>
                    <span className="recent-meta">{project.github_url.replace("https://github.com/", "")}</span>
                  </div>
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
