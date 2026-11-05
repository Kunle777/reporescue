import { useCallback, useEffect, useRef, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { useAuth } from "./auth/AuthContext";
import { useProfile } from "./auth/ProfileContext";
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
import { navigate, readRoute, subscribeToHash, type Route } from "./lib/router";
import type { Project, View } from "./lib/types";
import { isTerminalStatus } from "./lib/types";
import fullLogo from "./assets/RepoRescue-Logos/RepoRescue-full-logo.png";

export default function App() {
  const {
    session,
    user,
    loading: authLoading,
    signOut,
    recoveryPending,
  } = useAuth();
  const { username, refresh: refreshProfile } = useProfile();
  // The address-bar hash is the navigation source of truth: views and the
  // selected investigation survive refresh and honor back/forward buttons.
  const [route, setRoute] = useState<Route>(() => readRoute());
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectsError, setProjectsError] = useState("");
  const projectRequest = useRef(0);

  const view: View = route.view;

  useEffect(() => subscribeToHash(() => setRoute(readRoute())), []);

  const goView = useCallback(
    (next: View) => {
      navigate({
        view: next,
        jobId: next === "workbench" ? readRoute().jobId : null,
      });
    },
    []
  );

  const goNewInvestigation = useCallback(() => {
    navigate({ view: "workbench", jobId: null, newInvestigation: true });
  }, []);

  const openInvestigation = useCallback((jobId: string) => {
    navigate({ view: "workbench", jobId });
  }, []);

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

  // Restore the backend-owned active investigation after refresh/restart,
  // unless the URL already selects an investigation. Read-only: restoring
  // never creates a duplicate investigation.
  useEffect(() => {
    const currentRoute = readRoute();
    if (!session || currentRoute.jobId || currentRoute.newInvestigation) return;
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
          navigate({ view: "workbench", jobId: detail.job.job_id }, true);
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
  }, [session, signOut, route.jobId, route.newInvestigation, route.view]);

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
      <Sidebar view={view} onNavigate={goView} />
      <div className="main">
        <Topbar view={view} email={user?.email ?? null} username={username} onSignOut={() => void signOut()} onStartInvestigation={goNewInvestigation} />
        <main className="content">
          {view === "dashboard" && (
            <DashboardView
              username={username}
              projects={projects}
              loading={projectsLoading}
              error={projectsError}
              onRetry={() => void loadProjects()}
              onGoProjects={() => goView("projects")}
              onGoWorkbench={() => goView("workbench")}
              onOpenInvestigation={openInvestigation}
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
              key={`${user?.id ?? "signed-out"}:${route.newInvestigation ? "new" : route.jobId ?? "default"}` }
              session={session}
              projects={projects}
              projectsLoading={projectsLoading}
              onNavigateProjects={() => goView("projects")}
              onUnauthorized={handleUnauthorized}
              selectedJobId={route.jobId}
              onJobSelected={(jobId) =>
                navigate({ view: "workbench", jobId })
              }
            />
          </div>
          {view === "settings" && (
            <SettingsView
              user={user}
              session={session}
              onSignOut={() => void signOut()}
              onProfileChanged={() => void refreshProfile()}
            />
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
      <img src={fullLogo} alt="RepoRescue — Investigate, Debug, Fix" className="auth-logo" />
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
