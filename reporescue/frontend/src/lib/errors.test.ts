import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { friendlyError, reasonTitle } from "./errors";

describe("friendlyError", () => {
  it("maps known backend codes to titles and retry flags", () => {
    expect(friendlyError(new ApiError(422, "Debug is Python-only.", "DEBUG_PYTHON_ONLY"), "T", "F")).toEqual({
      title: "Debug is Python-only in beta",
      message: "Debug is Python-only.",
      retryable: false,
    });
    expect(friendlyError(new ApiError(429, "Slow down.", "PROVIDER_RATE_LIMITED"), "T", "F")).toEqual({
      title: "AI provider rate limit",
      message: "Slow down.",
      retryable: true,
    });
    expect(friendlyError(new ApiError(429, "Daily cap.", "BETA_DAILY_AI_LIMIT_REACHED"), "T", "F")).toEqual({
      title: "Daily AI limit reached",
      message: "Daily cap.",
      retryable: false,
    });
  });

  it("treats timeouts and network failures as retryable", () => {
    const friendly = friendlyError(new ApiError(0, "timed out"), "T", "F");
    expect(friendly.retryable).toBe(true);
    expect(friendly.message).toContain("Check your connection");
  });

  it("retries unknown 502s but not unknown 400s", () => {
    expect(friendlyError(new ApiError(502, "bad gateway"), "T", "F").retryable).toBe(true);
    expect(friendlyError(new ApiError(400, "bad request"), "T", "F")).toEqual({
      title: "T",
      message: "bad request",
      retryable: false,
    });
  });

  it("never leaks unexpected exception text", () => {
    expect(friendlyError(new TypeError("Cannot read x of undefined"), "T", "Safe fallback.")).toEqual({
      title: "T",
      message: "Safe fallback.",
      retryable: false,
    });
  });
});

describe("reasonTitle", () => {
  it("maps known persisted reasons and passes the rest through", () => {
    expect(reasonTitle("model_not_found")).toBe("AI model unavailable");
    expect(reasonTitle("provider_auth")).toBe("AI provider access problem");
    expect(reasonTitle("stage_failed")).toBe("stage_failed");
  });
});
