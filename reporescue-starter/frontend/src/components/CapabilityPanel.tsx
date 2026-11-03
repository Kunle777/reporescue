import type { CapabilityProfile, RepoProfile } from "../lib/types";

/**
 * Repository capability display from an ingestion profile. Renders only
 * backend-provided data: languages, manifests, frameworks, test signals,
 * and runnable-vs-static availability. Static-only is informational
 * (muted), never presented as a failure.
 */
export default function CapabilityPanel({ profile, warnings = [] }: { profile: RepoProfile; warnings?: string[] }) {
  const caps: CapabilityProfile | undefined = profile.capabilities;
  const runnable = caps?.runnable.python ?? profile.supported;
  const languages = caps ? Object.entries(caps.languages) : [];
  const signals = caps?.test_signals?.javascript;
  const frameworks = profile.frameworks ?? [];
  const depFiles = profile.dependency_files ?? [];

  return (
    <div className="card">
      <div className="card-head">
        <h3>
          {profile.name} <span className="mono muted">{profile.commit_sha.slice(0, 7)}</span>
        </h3>
        {runnable ? (
          <span className="pill green">runnable</span>
        ) : (
          <span className="pill">static only</span>
        )}
      </div>
      <dl className="kv-list">
        <div className="kv-row">
          <dt>Primary language</dt>
          <dd>{profile.primary_language}</dd>
        </div>
        <div className="kv-row">
          <dt>Languages</dt>
          <dd>
            {languages.length > 0 ? (
              languages.map(([name, info]) => (
                <span key={name} className="chip">
                  {name} · {info.files} file{info.files === 1 ? "" : "s"}
                  {info.manifest ? ` · ${info.manifest}` : ""}
                </span>
              ))
            ) : (
              <span className="muted">
                {profile.primary_language} · {profile.python_file_count} Python file
                {profile.python_file_count === 1 ? "" : "s"}
              </span>
            )}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Frameworks</dt>
          <dd>
            {frameworks.length > 0 ? (
              frameworks.join(", ")
            ) : (
              <span className="muted">none detected</span>
            )}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Dependencies</dt>
          <dd>
            {depFiles.length > 0 ? (
              depFiles.join(", ")
            ) : (
              <span className="muted">no manifest{profile.dependency_format === "none" ? "" : ` (${profile.dependency_format})`}</span>
            )}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Tests</dt>
          <dd>
            {profile.tests.count} Python test file{profile.tests.count === 1 ? "" : "s"}
            {profile.test_framework !== "none" ? ` (${profile.test_framework})` : ""}
            {signals && (signals.test_signals.length > 0 || signals.has_test_script) && (
              <span className="muted">
                {" "}· JS tooling: {signals.test_signals.join(", ") || "unlisted"}
                {signals.has_test_script ? " · npm test" : ""} (inspection only)
              </span>
            )}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Execution</dt>
          <dd>
            {runnable ? (
              <span>
                <span className="pill green">available</span>{" "}
                <span className="muted">Docker-backed pytest/unittest verification.</span>
              </span>
            ) : (
              <span>
                <span className="pill">unavailable</span>{" "}
                <span className="muted">
                  RepoRescue executes Python only — static analysis is fully available.
                </span>
              </span>
            )}
          </dd>
        </div>
      </dl>
      <p className="hint" style={{ marginTop: "8px" }}>{profile.structure_summary}</p>
      {warnings.length > 0 && <pre className="output">{warnings.join("\n")}</pre>}
    </div>
  );
}
