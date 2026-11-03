import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import CapabilityPanel from "./CapabilityPanel";
import type { CapabilityProfile, RepoProfile } from "../lib/types";

function profile(overrides: Record<string, unknown> = {}): RepoProfile {
  return {
    url: "https://github.com/acme/calc",
    name: "acme/calc",
    commit_sha: "a".repeat(40),
    ref: "main",
    primary_language: "python",
    supported: true,
    python_requires: ">=3.11",
    dependency_format: "pyproject",
    dependency_files: ["pyproject.toml"],
    frameworks: ["django"],
    test_framework: "pytest",
    tests: { dirs: ["tests"], files: ["tests/test_calc.py"], count: 12 },
    python_file_count: 48,
    source_dirs: ["pkg"],
    structure_summary: "Python project with 48 source files.",
    warnings: [],
    ...overrides,
  } as RepoProfile;
}

function capabilities(overrides: Record<string, unknown> = {}): CapabilityProfile {
  return {
    languages: { python: { files: 48, manifest: "pyproject.toml" } },
    dependencies: {},
    test_frameworks: ["pytest"],
    static: { available: true },
    runnable: { python: true, reason: null },
    ...overrides,
  } as CapabilityProfile;
}

describe("CapabilityPanel", () => {
  it("shows a runnable repository with its capabilities", () => {
    const { container } = render(<CapabilityPanel profile={profile()} warnings={[]} />);
    expect(screen.getByText("acme/calc")).toBeDefined();
    expect(screen.getByText("runnable")).toBeDefined();
    expect(screen.getByText("python")).toBeDefined();
    expect(screen.getByText(/python · 48 Python files/)).toBeDefined();
    expect(screen.getByText("django")).toBeDefined();
    expect(screen.getByText("pyproject.toml")).toBeDefined();
    expect(screen.getByText(/12 Python test files \(pytest\)/)).toBeDefined();
    expect(screen.getByText("available")).toBeDefined();
    expect(screen.getByText("Python project with 48 source files.")).toBeDefined();
    expect(container.querySelector(".pill.green")).toBeDefined();
    expect(screen.queryByText("static only")).toBeNull();
  });

  it("shows detected language chips and JavaScript signals", () => {
    render(
      <CapabilityPanel
        profile={profile({
          capabilities: capabilities({
            test_signals: { javascript: { test_signals: ["jest"], has_test_script: true } },
          }),
        })}
        warnings={[]}
      />
    );
    expect(screen.getByText(/python · 48 files · pyproject\.toml/)).toBeDefined();
    expect(screen.getByText(/JS tooling: jest · npm test \(inspection only\)/)).toBeDefined();
  });

  it("marks static-only repositories without failing them", () => {
    const { container } = render(
      <CapabilityPanel
        profile={profile({ capabilities: capabilities({ runnable: { python: false, reason: "no docker" } }) })}
        warnings={[]}
      />
    );
    expect(screen.getByText("static only")).toBeDefined();
    expect(screen.getByText("unavailable")).toBeDefined();
    expect(screen.getByText(/static analysis is fully available/)).toBeDefined();
    expect(container.querySelector(".pill.red")).toBeNull();
  });

  it("handles empty frameworks and dependencies", () => {
    render(
      <CapabilityPanel
        profile={profile({ frameworks: [], dependency_files: [] })}
        warnings={[]}
      />
    );
    expect(screen.getByText("none detected")).toBeDefined();
    expect(screen.getByText("no manifest (pyproject)")).toBeDefined();
  });

  it("shows warnings when present", () => {
    render(<CapabilityPanel profile={profile()} warnings={["w1", "w2"]} />);
    expect(screen.getByText(/w1/)).toBeDefined();
    expect(screen.getByText(/w2/)).toBeDefined();
  });
});
