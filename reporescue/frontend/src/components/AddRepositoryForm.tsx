import { useState, type FormEvent } from "react";
import { AlertTriangle, CheckCircle2, LoaderCircle, Plus } from "lucide-react";
import { ApiError } from "../lib/api";
import { createProject } from "../lib/projects";

/** Register a public GitHub repository via POST /api/projects. */
export default function AddRepositoryForm({
  onAdded,
  onUnauthorized,
}: {
  onAdded: () => void;
  onUnauthorized: () => void;
}) {
  const [url, setUrl] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  function validateUrl(raw: string): string | null {
    const value = raw.trim();
    if (!value) return "Enter a repository URL.";
    let parsed: URL;
    try {
      parsed = new URL(value);
    } catch {
      return "Enter a complete URL, e.g. https://github.com/owner/repo.";
    }
    if (parsed.protocol !== "https:") return "Use an https:// GitHub URL.";
    if (parsed.hostname.toLowerCase() !== "github.com") {
      return "Only public github.com repositories are supported.";
    }
    if (parsed.pathname.split("/").filter(Boolean).length < 2) {
      return "Include the owner and repository, e.g. https://github.com/owner/repo.";
    }
    return null;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const problem = validateUrl(url);
    if (problem) {
      setError(problem);
      setSuccess("");
      return;
    }
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const trimmedName = displayName.trim();
      const created = await createProject({
        github_url: url.trim(),
        ...(trimmedName ? { display_name: trimmedName } : {}),
      });
      setSuccess(`Registered ${created.display_name}.`);
      setUrl("");
      setDisplayName("");
      onAdded();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
      } else {
        setError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <form onSubmit={(event) => void submit(event)} className="form-stack">
        <div className="field">
          <label className="label" htmlFor="repo-url">
            Public GitHub URL
          </label>
          <input
            id="repo-url"
            className="input"
            type="url"
            required
            placeholder="https://github.com/owner/repo"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            autoComplete="off"
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="repo-display-name">
            Display name <span className="optional">(optional)</span>
          </label>
          <input
            id="repo-display-name"
            className="input"
            type="text"
            maxLength={200}
            placeholder="Defaults to the repository name"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            autoComplete="off"
          />
          <p className="hint">Editable anytime. The GitHub owner and name stay fixed.</p>
        </div>
        <div>
          <button className="btn btn-primary" type="submit" disabled={busy}>
            {busy ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />}
            {busy ? "Adding…" : "Add repository"}
          </button>
        </div>
      </form>
      {error && (
        <div className="banner banner-error">
          <AlertTriangle size={17} />
          <div>
            <strong>Could not add repository</strong>
            <p>{error}</p>
          </div>
        </div>
      )}
      {success && (
        <div className="banner banner-success">
          <CheckCircle2 size={17} />
          <div>
            <strong>Repository added</strong>
            <p>{success}</p>
          </div>
        </div>
      )}
    </div>
  );
}
