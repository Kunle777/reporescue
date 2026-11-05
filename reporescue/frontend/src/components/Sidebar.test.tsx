import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import Sidebar from "./Sidebar";

describe("Sidebar", () => {
  it("shows the RepoRescue logo and product name", () => {
    render(<Sidebar view="dashboard" onNavigate={() => {}} />);
    expect(screen.getByAltText("RepoRescue logo")).toBeDefined();
    expect(screen.getByText("RepoRescue")).toBeDefined();
  });

  it("navigates between views", () => {
    const onNavigate = vi.fn();
    render(<Sidebar view="dashboard" onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: /Workbench/ }));
    expect(onNavigate).toHaveBeenCalledWith("workbench");
  });
});
