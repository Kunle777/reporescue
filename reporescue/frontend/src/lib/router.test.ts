import { describe, expect, it } from "vitest";
import { buildHash, navigate, parseHash } from "./router";

describe("hash router", () => {
  it("parses an empty hash as the dashboard", () => {
    expect(parseHash("")).toEqual({ view: "dashboard", jobId: null });
    expect(parseHash("#/")).toEqual({ view: "dashboard", jobId: null });
  });

  it("parses plain view hashes", () => {
    expect(parseHash("#/projects")).toEqual({ view: "projects", jobId: null });
    expect(parseHash("#/workbench")).toEqual({ view: "workbench", jobId: null });
    expect(parseHash("#/settings")).toEqual({ view: "settings", jobId: null });
  });

  it("parses an investigation deep link", () => {
    expect(parseHash("#/workbench/job/abc-123")).toEqual({
      view: "workbench",
      jobId: "abc-123",
    });
  });

  it("falls back to the dashboard for unknown hashes", () => {
    expect(parseHash("#/nope")).toEqual({ view: "dashboard", jobId: null });
    expect(parseHash("#/workbench/job/")).toEqual({
      view: "workbench",
      jobId: null,
    });
  });

  it("round-trips routes through build and parse", () => {
    expect(parseHash(buildHash({ view: "dashboard", jobId: null }))).toEqual({
      view: "dashboard",
      jobId: null,
    });
    expect(
      parseHash(buildHash({ view: "workbench", jobId: "j1" }))
    ).toEqual({ view: "workbench", jobId: "j1" });
  });

  it("navigate writes the hash and is a no-op when already there", () => {
    window.location.hash = "#/dashboard";
    navigate({ view: "projects", jobId: null });
    expect(window.location.hash).toBe("#/projects");
    navigate({ view: "projects", jobId: null });
    expect(window.location.hash).toBe("#/projects");
    window.location.hash = "";
  });
});
