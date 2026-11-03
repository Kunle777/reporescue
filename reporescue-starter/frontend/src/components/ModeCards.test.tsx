import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import ModeCards from "./ModeCards";

describe("ModeCards", () => {
  it("renders all four modes with General marked as the default", () => {
    render(<ModeCards value="general" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: /General/ })).toBeDefined();
    expect(screen.getByRole("radio", { name: /Debug/ })).toBeDefined();
    expect(screen.getByRole("radio", { name: /Explain/ })).toBeDefined();
    expect(screen.getByRole("radio", { name: /Health/ })).toBeDefined();
    expect(screen.getByText("DEFAULT")).toBeDefined();
    expect(screen.getAllByRole("radio")).toHaveLength(4);
  });

  it("reflects the selected value", () => {
    render(<ModeCards value="health" onChange={() => {}} />);
    expect(screen.getByRole("radio", { name: /Health/ }).getAttribute("aria-checked")).toBe(
      "true"
    );
    expect(screen.getByRole("radio", { name: /General/ }).getAttribute("aria-checked")).toBe(
      "false"
    );
  });

  it("reports mode changes", () => {
    const onChange = vi.fn();
    render(<ModeCards value="general" onChange={onChange} />);
    fireEvent.click(screen.getByRole("radio", { name: /Debug/ }));
    expect(onChange).toHaveBeenCalledWith("debug");
  });

  it("disables every card while disabled", () => {
    render(<ModeCards value="general" onChange={() => {}} disabled />);
    for (const radio of screen.getAllByRole("radio")) {
      expect((radio as HTMLButtonElement).disabled).toBe(true);
    }
  });
});
