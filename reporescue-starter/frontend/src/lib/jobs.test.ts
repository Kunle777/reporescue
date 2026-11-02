import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { cancelJob, createJob, getActiveJob, getJob, listJobs } from "./jobs";

vi.mock("./api", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api")>();
  return { ...original, apiJson: vi.fn() };
});

import { apiJson } from "./api";

const mockedApiJson = vi.mocked(apiJson);

describe("jobs api client", () => {
  it("creates a job with mode and trimmed message", async () => {
    mockedApiJson.mockResolvedValueOnce({ job_id: "j1", investigation_id: "i1", status: "queued" });
    const result = await createJob({ ingestion_id: "abc", mode: "explain", message: "  why? " });
    expect(result.job_id).toBe("j1");
    expect(mockedApiJson).toHaveBeenCalledWith(
      "/api/jobs",
      expect.objectContaining({ method: "POST" })
    );
    const body = JSON.parse((mockedApiJson.mock.calls[0][1]?.body as string) ?? "{}");
    expect(body).toEqual({
      ingestion_id: "abc",
      mode: "explain",
      message: "why?",
      max_attempts: 3,
    });
  });

  it("sends null message when blank", async () => {
    mockedApiJson.mockResolvedValueOnce({ job_id: "j1", investigation_id: "i1", status: "queued" });
    await createJob({ ingestion_id: "abc", message: "   " });
    const body = JSON.parse((mockedApiJson.mock.calls[0][1]?.body as string) ?? "{}");
    expect(body.message).toBeNull();
  });

  it("returns the active investigation detail", async () => {
    mockedApiJson.mockResolvedValueOnce({ status: "running" });
    const detail = await getActiveJob();
    expect(detail).toEqual({ status: "running" });
    expect(mockedApiJson).toHaveBeenCalledWith("/api/jobs/active");
  });

  it("maps missing active job to null without throwing", async () => {
    mockedApiJson.mockRejectedValueOnce(new ApiError(404, "No active investigation."));
    await expect(getActiveJob()).resolves.toBeNull();
  });

  it("rethrows non-404 active errors", async () => {
    mockedApiJson.mockRejectedValueOnce(new ApiError(401, "nope"));
    await expect(getActiveJob()).rejects.toBeInstanceOf(ApiError);
  });

  it("fetches, lists, and cancels jobs", async () => {
    mockedApiJson.mockResolvedValueOnce({ status: "running" });
    await getJob("j1");
    expect(mockedApiJson).toHaveBeenCalledWith("/api/jobs/j1");

    mockedApiJson.mockResolvedValueOnce({ jobs: [{ job_id: "j1" }] });
    const jobs = await listJobs(5);
    expect(jobs).toEqual([{ job_id: "j1" }]);
    expect(mockedApiJson).toHaveBeenCalledWith("/api/jobs?limit=5");

    mockedApiJson.mockResolvedValueOnce({ status: "cancelled" });
    await cancelJob("j1");
    expect(mockedApiJson).toHaveBeenCalledWith(
      "/api/jobs/j1/cancel",
      expect.objectContaining({ method: "POST" })
    );
  });
});
