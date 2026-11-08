import { afterEach, describe, expect, it, vi } from "vitest";
import { apiJson, ApiError, throwForStatus } from "./api";

afterEach(() => vi.restoreAllMocks());

describe("shared FastAPI client responses", () => {
  it("handles a 204 response without attempting JSON parsing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    await expect(apiJson<void>("/api/projects/p1", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("returns undefined for an empty successful response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 200 })));
    await expect(apiJson<void>("/api/empty")).resolves.toBeUndefined();
  });

  it("retains an actionable validation message and the HTTP status", async () => {
    const response = new Response(JSON.stringify({ detail: [{ msg: "invalid repository URL" }] }), {
      status: 422,
      headers: { "Content-Type": "application/json" },
    });
    await expect(throwForStatus(response)).rejects.toMatchObject({
      status: 422,
      message: expect.stringContaining("invalid repository URL"),
    } satisfies Partial<ApiError>);
  });

  it("carries structured backend codes with the backend message", async () => {
    const response = new Response(
      JSON.stringify({ detail: { code: "DEBUG_PYTHON_ONLY", message: "Debug is Python-only." } }),
      { status: 422, headers: { "Content-Type": "application/json" } }
    );
    await expect(throwForStatus(response)).rejects.toMatchObject({
      status: 422,
      code: "DEBUG_PYTHON_ONLY",
      message: "Debug is Python-only.",
    } satisfies Partial<ApiError>);
  });
});
