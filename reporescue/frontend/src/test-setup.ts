import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// jsdom + explicit imports (no vitest globals): unmount after each test.
afterEach(() => {
  cleanup();
});
