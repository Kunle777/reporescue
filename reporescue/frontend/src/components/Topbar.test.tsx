import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import Topbar from "./Topbar";
import { ThemeProvider } from "../theme/ThemeContext";

function renderTopbar(username: string | null) {
  return render(
    <ThemeProvider>
      <Topbar
        view="dashboard"
        email="ada@example.com"
        username={username}
        onSignOut={() => {}}
        onStartInvestigation={() => {}}
      />
    </ThemeProvider>
  );
}

describe("Topbar account display", () => {
  it("shows the persisted username when available", () => {
    renderTopbar("ada_debugs");
    expect(screen.getByText("@ada_debugs")).toBeDefined();
  });

  it("falls back to the email when no username exists", () => {
    renderTopbar(null);
    expect(screen.getByText("ada@example.com")).toBeDefined();
  });

  it("cycles the theme preference from the navigation", () => {
    window.localStorage.clear();
    renderTopbar("ada_debugs");
    const toggle = screen.getByRole("button", { name: /Theme: system/ });
    fireEvent.click(toggle);
    expect(window.localStorage.getItem("reporescue-theme")).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
  });
});
