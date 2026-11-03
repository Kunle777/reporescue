import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import DemoPanel from "./DemoPanel";

const mockApi = vi.hoisted(() => ({ apiJson: vi.fn() }));
vi.mock("../lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/api")>();
  return { ...original, apiJson: mockApi.apiJson };
});

function demoPayload() {
  return {
    status: "complete",
    scenario: "negative-discount",
    repository: "demo/negative-discount",
    mode: "deterministic-local-demo",
    summary: "Fixed the discount bug.",
    verdict: "TESTS_PASSED",
    baseline: {
      status: "test_failure",
      passed: false,
      exit_code: 1,
      output: "baseline output",
      duration_ms: 12,
      failures: [{ test: "t", kind: "failed", message: "m", traceback: "tb" }],
    },
    patch: { file: "pkg/cart.py", before: "old", after: "new", reason: "why" },
    verification: {
      status: "ok",
      passed: true,
      exit_code: 0,
      output: "ok",
      duration_ms: 9,
      failures: [],
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("DemoPanel", () => {
  it("shows initial empty states before the first run", () => {
    render(<DemoPanel />);
    expect(screen.getByText("Public demo")).toBeDefined();
    expect(screen.getByText("Not run yet")).toBeDefined();
    expect(screen.getByText("Your patch will appear here")).toBeDefined();
    expect(screen.getByText("Evidence, not just an AI answer")).toBeDefined();
    expect(mockApi.apiJson).not.toHaveBeenCalled();
  });

  it("runs the demo without authentication and shows the verified fix", async () => {
    mockApi.apiJson.mockResolvedValueOnce(demoPayload());
    render(<DemoPanel />);
    fireEvent.click(screen.getByText("Run demo investigation"));
    expect(mockApi.apiJson).toHaveBeenCalledWith(
      "/api/demo/investigate",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario: "negative-discount" }),
      },
      { auth: false }
    );
    expect(await screen.findByText("Test-backed fix")).toBeDefined();
    expect(screen.getByText("Tests passed")).toBeDefined();
    expect(screen.getByText("pkg/cart.py")).toBeDefined();
    expect(screen.getByText("why")).toBeDefined();
    expect(screen.getByText("Deterministic demo patch — not model-generated.")).toBeDefined();
    fireEvent.click(screen.getByText("View raw test output"));
    expect(screen.getByText(/baseline output/)).toBeDefined();
  });

  it("shows demo failures with the backend hint", async () => {
    mockApi.apiJson.mockRejectedValueOnce(new Error("demo down"));
    render(<DemoPanel />);
    fireEvent.click(screen.getByText("Run demo investigation"));
    expect(await screen.findByText("Could not complete investigation")).toBeDefined();
    expect(screen.getByText(/demo down/)).toBeDefined();
    expect(screen.getByText(/port 8000/)).toBeDefined();
    expect(screen.queryByText("Test-backed fix")).toBeNull();
  });

  it("surfaces error reports from the demo endpoint", async () => {
    mockApi.apiJson.mockResolvedValueOnce({ status: "error", message: "bad scenario" });
    render(<DemoPanel />);
    fireEvent.click(screen.getByText("Run demo investigation"));
    expect(await screen.findByText("Could not complete investigation")).toBeDefined();
    expect(screen.getByText(/bad scenario/)).toBeDefined();
  });

  it("disables the button while the demo runs", async () => {
    let resolve!: (value: unknown) => void;
    mockApi.apiJson.mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      })
    );
    render(<DemoPanel />);
    fireEvent.click(screen.getByText("Run demo investigation"));
    expect(screen.getByText("Running checks…")).toBeDefined();
    expect((screen.getByText("Running checks…") as HTMLButtonElement).disabled).toBe(true);
    resolve(demoPayload());
    expect(await screen.findByText("Test-backed fix")).toBeDefined();
  });
});
