import type { View } from "./types";

export type Route = {
  view: View;
  /** Selected investigation job id (workbench only, else null). */
  jobId: string | null;
};

const VIEWS: View[] = ["dashboard", "projects", "workbench", "settings"];

/** Parse a location hash into a route. Unknown hashes land on the dashboard. */
export function parseHash(hash: string): Route {
  const clean = hash.startsWith("#") ? hash.slice(1) : hash;
  const parts = clean.split("/").filter(Boolean);
  const [head, ...rest] = parts;
  if (head === "workbench" && rest[0] === "job" && rest[1]) {
    return { view: "workbench", jobId: decodeURIComponent(rest[1]) };
  }
  if (head && (VIEWS as string[]).includes(head)) {
    return { view: head as View, jobId: null };
  }
  return { view: "dashboard", jobId: null };
}

/** Build the canonical hash for a route (deep-linkable, refresh-safe). */
export function buildHash(route: Route): string {
  if (route.view === "workbench" && route.jobId) {
    return `#/workbench/job/${encodeURIComponent(route.jobId)}`;
  }
  return `#/${route.view}`;
}

/** Read the current route from the browser address bar. */
export function readRoute(): Route {
  return parseHash(window.location.hash);
}

/**
 * Navigate to a route by updating the hash (back/forward button safe).
 * No-op when already there. `replace` swaps the history entry instead
 * of pushing (used for post-login restores, not user navigation).
 */
export function navigate(route: Route, replace = false): void {
  const hash = buildHash(route);
  if (window.location.hash === hash) return;
  if (replace) window.location.replace(hash);
  else window.location.hash = hash;
}

/** Subscribe to back/forward and programmatic hash changes. */
export function subscribeToHash(listener: () => void): () => void {
  window.addEventListener("hashchange", listener);
  return () => window.removeEventListener("hashchange", listener);
}
