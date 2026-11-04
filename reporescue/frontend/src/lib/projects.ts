import { apiFetch, apiJson, throwForStatus } from "./api";
import type { Project } from "./types";

const JSON_HEADERS = { "Content-Type": "application/json" };

export type ProjectCreateInput = {
  github_url: string;
  display_name?: string;
  default_branch?: string;
};

/** GET /api/projects — the caller's persisted repositories, newest first. */
export function listProjects(): Promise<Project[]> {
  return apiJson<Project[]>("/api/projects", { method: "GET" });
}

/** POST /api/projects — register a public GitHub repository. */
export function createProject(input: ProjectCreateInput): Promise<Project> {
  return apiJson<Project>("/api/projects", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
}

/** PATCH /api/projects/{id} — rename (canonical identity is immutable). */
export function renameProject(id: string, display_name: string): Promise<Project> {
  return apiJson<Project>(`/api/projects/${id}`, {
    method: "PATCH",
    headers: JSON_HEADERS,
    body: JSON.stringify({ display_name }),
  });
}

/** DELETE /api/projects/{id} — 204 with no body on success. */
export async function deleteProject(id: string): Promise<void> {
  const response = await apiFetch(`/api/projects/${id}`, { method: "DELETE" });
  await throwForStatus(response);
}
