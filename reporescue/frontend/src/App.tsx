import { useCallback, useEffect, useRef, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { useAuth } from "./auth/AuthContext";
import Login, { ResetPasswordForm } from "./components/Login";
import Sidebar from "./components/Sidebar";
import Topbar from "./components/Topbar";
import DashboardView from "./views/DashboardView";
import ProjectsView from "./views/ProjectsView";
import SettingsView from "./views/SettingsView";
import WorkbenchView from "./views/WorkbenchView";
import { ApiError } from "./lib/api";
import { getActiveJob } from "./lib/jobs";
import { listProjects } from "./lib/projects";
import type { Project, View } from "./lib/types";
import { isTerminalStatus } from "./lib/types";

export default function App() {
  const {
    session,
    user,
    loading: authLoading,
    signOut,
    recoveryPending,
  } = useAuth();
  const [view, setView] = useState<View>("dashboard");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectsError, setProjectsError] = useState("");
  const [restoredJobId, setRestoredJobId] = useState<string | null>(null);
  const projectRequest = useRef(0);

  const loadProjects = useCallback(async () => {
    const requestId = ++projectRequest.current;
    setProjectsLoading(true);
    setProjectsError("");
    try {
      const nextProjects = await listProjects();
      if (requestId === projectRequest.current) setProjects(nextProjects);
    } catch (err) {
      if (requestId !== projectRequest.current) return;
      if (err instanceof ApiError && err.status === 401) {
        await signOut();
      } else {
        setProjectsError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
      }
    } finally {
      if (requestId === projectRequest.current) setProjectsLoading(false);
    }
  }, [signOut]);

  useEffect(() => {
    if (!session) {
      projectRequest.current += 1;
      setProjects([]);
      setProjectsError("");
      setProjectsLoading(false);
      return;
    }
    void loadProjects();
  }, [session, loadProjects]);

  // Restore the backend-owned active investigation after refresh/restart.
  // Read-only: restoring never creates a duplicate investigation.
  useEffect(() => {
    if (!session) {
      setRestoredJobId(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const detail = await getActiveJob();
        if (
          !cancelled &&
          detail &&
          detail.job &&
          !isTerminalStatus(detail.status)
        ) {
          setRestoredJobId(detail.job.job_id);
          setView("workbench");
        }
      } catch (err) {
        if (!cancelled && err instanceof ApiError && err.status === 401) {
          await signOut();
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [session, signOut]);

  const handleUnauthorized = useCallback(async () => {
    await signOut();
  }, [signOut]);

  if (authLoading) {
    return (
      <div className="auth-screen">
        <div className="auth-loading-panel"><span className="brand-mark"><ShieldCheck size={22} /></span><div className="loading-note">Restoring your RepoRescue session…</div><p className="company-attribution">Made by K. T Labs</p></div>
      </div>
    );
  }

  if (recoveryPending && session) {
    return (
      <div className="auth-screen">
        <div className="auth-layout">
          <AuthAside />
          <section className="auth-card">
            <div className="auth-form-heading">
              <h1>Set a new password</h1>
              <p>You arrived from a password-reset link.</p>
            </div>
            <ResetPasswordForm />
            <p className="company-attribution">Made by K. T Labs</p>
          </section>
        </div>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="auth-screen">
        <div className="auth-layout">
          <AuthAside />
          <section className="auth-card">
            <Login />
            <p className="company-attribution">Made by K. T Labs</p>
          </section>
        </div>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <Sidebar view={view} onNavigate={setView} />
      <div className="main">
        <Topbar view={view} email={user?.email ?? null} onSignOut={() => void signOut()} onStartInvestigation={() => setView("workbench")} />
        <main className="content">
          {view === "dashboard" && (
            <DashboardView
              userEmail={user?.email ?? null}
              projects={projects}
              loading={projectsLoading}
              error={projectsError}
              onRetry={() => void loadProjects()}
              onGoProjects={() => setView("projects")}
              onGoWorkbench={() => setView("workbench")}
              onOpenInvestigation={(id) => {
                setRestoredJobId(id);
                setView("workbench");
              }}
              onChanged={() => void loadProjects()}
              onUnauthorized={handleUnauthorized}
            />
          )}
          {view === "projects" && (
            <ProjectsView
              projects={projects}
              loading={projectsLoading}
              error={projectsError}
              onRetry={() => void loadProjects()}
              onChanged={() => void loadProjects()}
              onUnauthorized={handleUnauthorized}
            />
          )}
          <div hidden={view !== "workbench"}>
            <WorkbenchView
              key={user?.id ?? "signed-out"}
              session={session}
              projects={projects}
              projectsLoading={projectsLoading}
              onNavigateProjects={() => setView("projects")}
              onUnauthorized={handleUnauthorized}
              restoredJobId={restoredJobId}
              onJobAdopted={() => setRestoredJobId(null)}
            />
          </div>
          {view === "settings" && (
            <SettingsView user={user} session={session} onSignOut={() => void signOut()} />
          )}
        </main>
        <footer className="app-footer">Made by K. T Labs</footer>
      </div>
    </div>
  );
}

function AuthAside() {
  return (
    <aside className="auth-aside">
      <div className="auth-aside-brand">
        <span className="brand-mark"><ShieldCheck size={20} /></span>
        <span>RepoRescue</span>
      </div>
      <div className="auth-aside-copy">
        <p className="eyebrow">REPOSITORY ENGINEERING</p>
        <h2>Investigate with evidence. Verify with execution.</h2>
        <p>Understand a codebase, trace a problem, and review what the tests actually prove.</p>
      </div>
      <div className="auth-principles" aria-label="RepoRescue principles">
        <div><span>01</span><strong>Inspect the pinned revision</strong></div>
        <div><span>02</span><strong>Separate findings from hypotheses</strong></div>
        <div><span>03</span><strong>Trust execution evidence</strong></div>
      </div>
    </aside>
  );
}
