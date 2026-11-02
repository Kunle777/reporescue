import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, ShieldCheck } from "lucide-react";
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
  const [showDemo, setShowDemo] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectsError, setProjectsError] = useState("");
  const [restoredJobId, setRestoredJobId] = useState<string | null>(null);

  const loadProjects = useCallback(async () => {
    setProjectsLoading(true);
    setProjectsError("");
    try {
      setProjects(await listProjects());
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        await signOut();
      } else {
        setProjectsError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
      }
    } finally {
      setProjectsLoading(false);
    }
  }, [signOut]);

  useEffect(() => {
    if (!session) {
      setProjects([]);
      setProjectsError("");
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

  async function handleUnauthorized() {
    await signOut();
  }

  if (authLoading) {
    return (
      <div className="auth-screen">
        <div className="loading-note">Loading session…</div>
      </div>
    );
  }

  if (recoveryPending && session) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <div className="auth-brand">
            <span className="brand-mark">
              <ShieldCheck size={22} />
            </span>
            <strong>Set a new password</strong>
            <p>You arrived from a password-reset link.</p>
          </div>
          <ResetPasswordForm />
        </div>
      </div>
    );
  }

  if (!session && !showDemo) {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <div className="auth-brand">
            <span className="brand-mark">
              <ShieldCheck size={22} />
            </span>
            <strong>RepoRescue</strong>
            <p>Sign in to manage repositories and run investigations.</p>
          </div>
          <Login />
          <button
            type="button"
            className="link-button auth-demo-link"
            onClick={() => setShowDemo(true)}
          >
            Continue to the public demo without signing in
          </button>
        </div>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="demo-shell">
        <div className="content">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => setShowDemo(false)}
          >
            <ArrowLeft size={14} /> Back to sign in
          </button>
          <WorkbenchView session={null} onUnauthorized={handleUnauthorized} />
        </div>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <Sidebar view={view} onNavigate={setView} />
      <div className="main">
        <Topbar view={view} email={user?.email ?? null} onSignOut={() => void signOut()} />
        <main className="content">
          {view === "dashboard" && (
            <DashboardView
              userEmail={user?.email ?? null}
              projects={projects}
              loading={projectsLoading}
              error={projectsError}
              onRetry={() => void loadProjects()}
              onGoProjects={() => setView("projects")}
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
          {view === "workbench" && (
            <WorkbenchView
              session={session}
              onUnauthorized={handleUnauthorized}
              restoredJobId={restoredJobId}
              onJobAdopted={() => setRestoredJobId(null)}
            />
          )}
          {view === "settings" && (
            <SettingsView user={user} session={session} onSignOut={() => void signOut()} />
          )}
        </main>
      </div>
    </div>
  );
}
