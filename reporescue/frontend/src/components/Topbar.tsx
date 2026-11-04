import { FlaskConical, LogOut } from "lucide-react";
import type { View } from "../lib/types";

const TITLES: Record<View, string> = {
  dashboard: "Dashboard",
  projects: "Projects",
  workbench: "Workbench",
  settings: "Settings",
};

export default function Topbar({
  view,
  email,
  onSignOut,
  onStartInvestigation,
}: {
  view: View;
  email: string | null;
  onSignOut: () => void;
  onStartInvestigation: () => void;
}) {
  return (
    <header className="topbar">
      <div className="topbar-left">
        <span className="topbar-context">Workspace</span>
        <span className="topbar-divider" aria-hidden="true">/</span>
        <h1 className="topbar-title">{TITLES[view]}</h1>
      </div>
      <div className="topbar-right">
        {view !== "workbench" && <button type="button" className="btn btn-primary btn-sm topbar-start" onClick={onStartInvestigation}><FlaskConical size={15} /> New investigation</button>}
        <div className="account-chip" title={email ?? "Account"}>
          <span className="account-initial" aria-hidden="true">{email?.[0]?.toUpperCase() ?? "A"}</span>
          {email && <span className="account-email">{email}</span>}
        </div>
        <button type="button" className="btn btn-quiet btn-sm sign-out-button" onClick={onSignOut}>
          <LogOut size={15} /> <span>Sign out</span>
        </button>
      </div>
    </header>
  );
}
