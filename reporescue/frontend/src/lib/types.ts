/** Shared API shapes. Project mirrors the backend ProjectResponse contract. */

export type View = "dashboard" | "projects" | "workbench" | "settings";

export type Project = {
  id: string;
  display_name: string;
  github_owner: string;
  repository_name: string;
  github_url: string;
  default_branch: string | null;
  visibility: string;
  created_at: string;
  updated_at: string;
};

export type TestRun = {
  passed: boolean;
  exit_code: number | null;
  output: string;
  duration_ms: number;
};

export type Report = {
  status: string;
  scenario: string;
  repository: string;
  mode: string;
  summary: string;
  verdict: string;
  baseline: TestRun;
  verification: TestRun;
  patch: { file: string; before: string; after: string; reason: string };
  limitations: string[];
};

export type RepoProfile = {
  url: string;
  name: string;
  commit_sha: string;
  ref: string | null;
  primary_language: string;
  supported: boolean;
  python_requires: string | null;
  dependency_format: string;
  dependency_files: string[];
  frameworks: string[];
  test_framework: string;
  tests: { dirs: string[]; files: string[]; count: number };
  python_file_count: number;
  source_dirs: string[];
  structure_summary: string;
  warnings: string[];
};

export type IngestResult = {
  status: string;
  ingestion_id: string;
  profile: RepoProfile;
  warnings: string[];
};

export type ExecFailure = {
  test: string;
  kind: string;
  message: string;
  traceback: string;
};

export type ExecResult = {
  backend: string;
  isolation: string;
  status: string;
  passed: boolean | null;
  exit_code: number | null;
  output: string;
  duration_ms: number;
  failures: ExecFailure[];
  install_ok: boolean | null;
  install_log: string;
};

export type RealReport = {
  status: string;
  repository: string;
  commit_sha: string;
  mode: string;
  summary: string;
  verdict: string;
  diagnosis: string;
  baseline: ExecResult;
  verification: ExecResult;
  patch: { edits: { file: string; before: string; after: string }[]; reason: string };
  guard: { validated: boolean; message: string };
  execution: { backend: string; isolation: string };
  attempts: number;
  limitations: string[];
};

export type JobStatus =
  | "queued"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "cancelled";

export type InvestigationMode = "debug" | "explain" | "health";

export type JobSummary = {
  job_id: string;
  investigation_id: string;
  status: JobStatus;
  attempts: number;
  max_attempts: number;
  error: { reason: string; message: string } | null;
  created_at: string | null;
  updated_at: string | null;
};

export type JobCard = JobSummary & {
  repo_name: string;
  mode: InvestigationMode;
  progress_pct: number;
};

export type InvestigationDetail = {
  investigation_id: string;
  repo_url: string;
  repo_name: string;
  commit_sha: string;
  ref: string | null;
  mode: InvestigationMode;
  user_message: string | null;
  status: JobStatus;
  stage: string | null;
  stages_completed: string[];
  progress_pct: number;
  partial: Record<string, unknown> | null;
  verification_status: string;
  error: { reason: string; message: string } | null;
  limitations: string[];
  final_report: Record<string, unknown> | null;
  timings: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
  job: JobSummary | null;
  attempts: {
    attempt_no: number;
    diagnosis: string | null;
    confidence: string;
    patch: Record<string, unknown> | null;
    guard: Record<string, unknown> | null;
    verification: Record<string, unknown> | null;
  }[];
  runs: {
    kind: string;
    backend: string | null;
    status: string | null;
    passed: boolean | null;
    exit_code: number | null;
    output: string | null;
    failures: ExecFailure[];
    duration_ms: number | null;
  }[];
  findings: {
    kind: string;
    category: string | null;
    title: string;
    detail: string | null;
    confidence: string;
  }[];
  patches: {
    status: string;
    reason: string | null;
    files: { file: string; before: string | null; after: string | null }[];
  }[];
  messages: { role: string; content: string }[];
};

export function isTerminalStatus(status: JobStatus): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}
