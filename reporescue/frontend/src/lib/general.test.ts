import { describe, expect, it, vi } from "vitest";
import { classifyObjective } from "./general";

vi.mock("./api", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api")>();
  return { ...original, apiJson: vi.fn() };
});

import { apiJson } from "./api";

const mockedApiJson = vi.mocked(apiJson);

describe("general classify client", () => {
  it("posts the objective to the classify endpoint", async () => {
    mockedApiJson.mockResolvedValueOnce({
      selected_mode: "debug",
      rationale: "Failure signals matched.",
      confidence: "high",
      signals: {},
      source: "deterministic",
    });
    const result = await classifyObjective("fix the failing tests");
    expect(result.selected_mode).toBe("debug");
    expect(mockedApiJson).toHaveBeenCalledWith(
      "/api/general/classify",
      expect.objectContaining({ method: "POST" })
    );
    const body = JSON.parse(
      (mockedApiJson.mock.calls[0][1]?.body as string) ?? "{}"
    );
    expect(body).toEqual({ message: "fix the failing tests" });
  });

  it("passes clarification outcomes through untouched", async () => {
    mockedApiJson.mockResolvedValueOnce({
      selected_mode: null,
      reason: "needs_clarification",
      question: "What should be investigated?",
      rationale: "Too vague.",
      confidence: "none",
      signals: {},
      suggested_mode: "debug",
      source: "deterministic",
    });
    const result = await classifyObjective("debug it");
    expect(result.selected_mode).toBeNull();
  });
});
