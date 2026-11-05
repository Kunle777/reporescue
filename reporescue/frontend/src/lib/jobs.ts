import { ApiError, apiJson } from "./api";
import type { ApiInvestigationMode, InvestigationDetail, JobCard } from "./types";

export type CreateJobInput = {
  ingestion_id: string;
  mode?: ApiInvestigationMode;
  message?: string;
  max_attempts?: number;
  project_id?: string;
  /** Pre-classified General route (from classifyObjective). */
  selected_mode?: ApiInvestigationMode;
  routing_rationale?: string;
  routing_confidence?: string;
  routing_source?: string;
  idempotencyKey?: string;
};

export type CreatedJob = {
  job_id: string;
  investigation_id: string;
  status: string;
};

/** Enqueue a persistent background investigation. */
export async function createJob(input: CreateJobInput): Promise<CreatedJob> {
  return apiJson<CreatedJob>("/api/jobs", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(input.idempotencyKey ? { "Idempotency-Key": input.idempotencyKey } : {}),
    },
    body: JSON.stringify({
      ingestion_id: input.ingestion_id,
      mode: input.mode ?? "debug",
      message: input.message?.trim() ? input.message.trim() : null,
      max_attempts: input.max_attempts ?? 3,
      ...(input.project_id ? { project_id: input.project_id } : {}),
      ...(input.selected_mode ? { selected_mode: input.selected_mode } : {}),
      ...(input.routing_rationale
        ? { routing_rationale: input.routing_rationale }
        : {}),
      ...(input.routing_confidence
        ? { routing_confidence: input.routing_confidence }
        : {}),
      ...(input.routing_source ? { routing_source: input.routing_source } : {}),
    }),
  });
}

/** Most recently updated non-terminal job, or null when there is none. */
export async function getActiveJob(): Promise<InvestigationDetail | null> {
  try {
    return await apiJson<InvestigationDetail>("/api/jobs/active");
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

/** Full investigation/job state for one job (poll target). */
export async function getJob(jobId: string): Promise<InvestigationDetail> {
  return apiJson<InvestigationDetail>(`/api/jobs/${jobId}`);
}

/** Newest-first job cards for the owner. */
export async function listJobs(limit = 10): Promise<JobCard[]> {
  const data = await apiJson<{ jobs: JobCard[] }>(
    `/api/jobs?limit=${encodeURIComponent(String(limit))}`
  );
  return data.jobs;
}

/** Cancel a non-terminal job. Terminal jobs are returned unchanged. */
export async function cancelJob(jobId: string): Promise<InvestigationDetail> {
  return apiJson<InvestigationDetail>(`/api/jobs/${jobId}/cancel`, {
    method: "POST",
  });
}
