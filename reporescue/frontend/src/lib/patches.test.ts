import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { downloadInvestigationPatch } from "./patches";

vi.mock("./api", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api")>();
  return { ...original, apiFetch: vi.fn() };
});

import { apiFetch } from "./api";

const mockedApiFetch = vi.mocked(apiFetch);

function okResponse(body: string, filename: string | null) {
  const headers: Record<string, string> = {};
  if (filename) headers["Content-Disposition"] = `attachment; filename="${filename}"`;
  return new Response(body, { status: 200, headers });
}

describe("patches download client", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
  });

  it("downloads the attachment with the server filename", async () => {
    mockedApiFetch.mockResolvedValueOnce(
      okResponse("diff", "reporescue-acme-x1-i1-verified.patch")
    );
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const result = await downloadInvestigationPatch("i1");
    expect(result.filename).toBe("reporescue-acme-x1-i1-verified.patch");
    expect(mockedApiFetch).toHaveBeenCalledWith("/api/investigations/i1/patch");
    expect(click).toHaveBeenCalledTimes(1);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock");
    click.mockRestore();
  });

  it("falls back when the filename header is missing", async () => {
    mockedApiFetch.mockResolvedValueOnce(okResponse("diff", null));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const result = await downloadInvestigationPatch("i1");
    expect(result.filename).toBe("reporescue-i1.patch");
    click.mockRestore();
  });

  it.each([
    [404, "No downloadable patch is available for this investigation."],
    [422, "The stored patch does not match the pinned snapshot, so no file was generated."],
    [413, "The patch exceeds the download size limit."],
    [401, "Your session is no longer valid. Sign in again."],
  ])("maps HTTP %i to a friendly error", async (status, message) => {
    mockedApiFetch.mockResolvedValueOnce(new Response("nope", { status }));
    await expect(downloadInvestigationPatch("i1")).rejects.toMatchObject({ status, message });
  });

  it("maps network failure to a connection error", async () => {
    mockedApiFetch.mockRejectedValueOnce(new ApiError(0, "down"));
    await expect(downloadInvestigationPatch("i1")).rejects.toMatchObject({ status: 0 });
  });
});
