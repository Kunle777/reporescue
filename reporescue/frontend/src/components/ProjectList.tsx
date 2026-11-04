import { useState } from "react";
import {
  AlertTriangle,
  Check,
  ExternalLink,
  FolderGit2,
  Pencil,
  Trash2,
  X,
} from "lucide-react";
import { ApiError } from "../lib/api";
import { deleteProject, renameProject } from "../lib/projects";
import type { Project } from "../lib/types";

/** Persisted repositories from GET /api/projects, with rename and delete. */
export default function ProjectList({
  projects,
  compact = false,
  onChanged,
  onUnauthorized,
}: {
  projects: Project[];
  compact?: boolean;
  onChanged: () => void;
  onUnauthorized: () => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");

  function fail(err: unknown) {
    if (err instanceof ApiError && err.status === 401) {
      onUnauthorized();
    } else {
      setError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
    }
  }

  async function saveRename(project: Project) {
    const name = draft.trim();
    if (!name) {
      setError("Display name cannot be empty.");
      return;
    }
    setBusyId(project.id);
    setError("");
    try {
      await renameProject(project.id, name);
      setEditingId(null);
      onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setBusyId(null);
    }
  }

  async function remove(project: Project) {
    setBusyId(project.id);
    setError("");
    try {
      await deleteProject(project.id);
      setConfirmId(null);
      onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div>
      {error && (
        <div className="banner banner-error">
          <AlertTriangle size={17} />
          <div>
            <strong>Something went wrong</strong>
            <p>{error}</p>
          </div>
        </div>
      )}
      <ul className="list">
        {projects.map((project) => (
          <li key={project.id} className="list-row">
            <span className="row-icon">
              <FolderGit2 size={17} />
            </span>
            <div className="row-main">
              {editingId === project.id ? (
                <span className="row-edit">
                  <input
                    className="input input-sm"
                    value={draft}
                    maxLength={200}
                    onChange={(event) => setDraft(event.target.value)}
                    aria-label="Display name"
                    autoFocus
                  />
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    disabled={busyId === project.id}
                    onClick={() => void saveRename(project)}
                    aria-label="Save name"
                  >
                    <Check size={14} />
                  </button>
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    disabled={busyId === project.id}
                    onClick={() => setEditingId(null)}
                    aria-label="Cancel rename"
                  >
                    <X size={14} />
                  </button>
                </span>
              ) : (
                <>
                  <strong>{project.display_name}</strong>
                  <small>
                    {project.github_owner}/{project.repository_name}
                    {project.default_branch ? ` · ${project.default_branch}` : ""}
                    {" · added "}
                    {new Date(project.created_at).toLocaleDateString()}
                  </small>
                </>
              )}
            </div>
            {!compact && editingId !== project.id && (
              <span className="row-actions">
                <a
                  className="btn btn-secondary btn-sm"
                  href={project.github_url}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`Open ${project.display_name} on GitHub`}
                >
                  <ExternalLink size={14} />
                </a>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={busyId === project.id}
                  onClick={() => {
                    setEditingId(project.id);
                    setDraft(project.display_name);
                    setConfirmId(null);
                  }}
                  aria-label={`Rename ${project.display_name}`}
                >
                  <Pencil size={14} />
                </button>
                {confirmId === project.id ? (
                  <>
                    <button
                      type="button"
                      className="btn btn-danger btn-sm"
                      disabled={busyId === project.id}
                      onClick={() => void remove(project)}
                    >
                      Confirm
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busyId === project.id}
                      onClick={() => setConfirmId(null)}
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    disabled={busyId === project.id}
                    onClick={() => {
                      setConfirmId(project.id);
                      setEditingId(null);
                    }}
                    aria-label={`Delete ${project.display_name}`}
                  >
                    <Trash2 size={14} />
                  </button>
                )}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
