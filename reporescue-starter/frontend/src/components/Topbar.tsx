import { LogOut, ShieldCheck } from "lucide-react";
import type { View } from "../lib/types";

const TITLES: Record<View, string> = {
  dashboard: "Dashboard",
  projects: "Projects",
  workbench: "Workbench",
  "new-investigation": "New Investigation",
  settings: "Settings",
};

export default function Topbar({
  view,
  email,
  onSignOut,
}: {
  view: View;
  email: string | null;
  onSignOut: () => void;
}) {
  return (
    <header className="topbar">
      <div className="topbar-left">
        <span className="topbar-brand">
          <ShieldCheck size={17} /> RepoRescue
        </span>
        <span className="topbar-divider" aria-hidden="true" />
        <h1 className="topbar-title">{TITLES[view]}</h1>
      </div>
      <div className="topbar-right">
        {email && <span className="account-email">{email}</span>}
        <button type="button" className="btn btn-secondary btn-sm" onClick={onSignOut}>
          <LogOut size={14} /> Sign out
        </button>
      </div>
    </header>
  );
}
