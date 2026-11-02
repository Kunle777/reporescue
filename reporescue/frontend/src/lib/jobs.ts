import { ApiError, apiJson } from "./api";
import type { InvestigationDetail, InvestigationMode, JobCard } from "./types";

export type CreateJobInput = {
  ingestion_id: string;
  mode?: InvestigationMode;
  message?: string;
  max_attempts?: number;
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
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ingestion_id: input.ingestion_id,
      mode: input.mode ?? "debug",
      message: input.message?.trim() ? input.message.trim() : null,
      max_attempts: input.max_attempts ?? 3,
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
