import {
  FlaskConical,
  FolderGit2,
  LayoutDashboard,
  Settings as SettingsIcon,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import type { View } from "../lib/types";

const ITEMS: { id: View; label: string; icon: LucideIcon }[] = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "projects", label: "Repositories", icon: FolderGit2 },
  { id: "workbench", label: "Workbench", icon: FlaskConical },
  { id: "settings", label: "Settings", icon: SettingsIcon },
];

export default function Sidebar({
  view,
  onNavigate,
}: {
  view: View;
  onNavigate: (view: View) => void;
}) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark">
          <ShieldCheck size={20} />
        </span>
        <span className="brand-lockup">
          <span className="brand-name">RepoRescue</span>
          <span className="brand-caption">Repository engineering</span>
        </span>
      </div>
      <span className="nav-caption">WORKSPACE</span>
      <nav className="nav" aria-label="Primary">
        {ITEMS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`nav-item${view === item.id ? " active" : ""}`}
            onClick={() => onNavigate(item.id)}
            aria-current={view === item.id ? "page" : undefined}
          >
            <item.icon size={17} /> {item.label}
          </button>
        ))}
      </nav>
    </aside>
  );
}
