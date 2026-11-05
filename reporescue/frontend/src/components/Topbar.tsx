import { FlaskConical, LogOut, Monitor, Moon, Sun } from "lucide-react";
import type { View } from "../lib/types";
import { useTheme, type ThemePreference } from "../theme/ThemeContext";

const TITLES: Record<View, string> = {
  dashboard: "Dashboard",
  projects: "Repositories",
  workbench: "Workbench",
  settings: "Settings",
};

export default function Topbar({
  view,
  email,
  username,
  onSignOut,
  onStartInvestigation,
}: {
  view: View;
  email: string | null;
  username: string | null;
  onSignOut: () => void;
  onStartInvestigation: () => void;
}) {
  const displayName = username ? `@${username}` : email;
  const initial = (username ?? email ?? "A")[0]?.toUpperCase() ?? "A";
  const { preference, setPreference } = useTheme();
  const order: ThemePreference[] = ["system", "light", "dark"];
  const next = order[(order.indexOf(preference) + 1) % order.length];
  const ThemeIcon = preference === "light" ? Sun : preference === "dark" ? Moon : Monitor;
  return (
    <header className="topbar">
      <div className="topbar-left">
        <span className="topbar-context">Workspace</span>
        <span className="topbar-divider" aria-hidden="true">/</span>
        <h1 className="topbar-title">{TITLES[view]}</h1>
      </div>
      <div className="topbar-right">
        <button type="button" className="btn btn-primary btn-sm topbar-start" onClick={onStartInvestigation}><FlaskConical size={15} /> <span>New investigation</span></button>
        <button type="button" className="btn btn-quiet btn-sm" title={`Theme: ${preference} (switch to ${next})`} aria-label={`Theme: ${preference}. Activate to switch to ${next}.`} onClick={() => setPreference(next)}>
          <ThemeIcon size={15} />
        </button>
        <div className="account-chip" title={email ?? displayName ?? "Account"}>
          <span className="account-initial" aria-hidden="true">{initial}</span>
          {displayName && <span className="account-email">{displayName}</span>}
        </div>
        <button type="button" className="btn btn-quiet btn-sm sign-out-button" onClick={onSignOut}>
          <LogOut size={15} /> <span>Sign out</span>
        </button>
      </div>
    </header>
  );
}
