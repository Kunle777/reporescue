import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import Login, { ResetPasswordForm } from "./Login";

const mockAuth = vi.hoisted(() => ({
  configured: true,
  session: null,
  user: null,
  loading: false,
  recoveryPending: false,
  callbackError: null as string | null,
  clearCallbackError: vi.fn(),
  signUp: vi.fn(),
  signIn: vi.fn(),
  signInWithGitHub: vi.fn(),
  signOut: vi.fn(),
  resendConfirmation: vi.fn(),
  requestPasswordReset: vi.fn(),
  updatePassword: vi.fn(),
}));

vi.mock("../auth/AuthContext", () => ({ useAuth: () => mockAuth }));

function signupFields() {
  return {
    username: screen.getByLabelText("Username"),
    email: screen.getByLabelText("Email address"),
    password: screen.getByLabelText("Password"),
    confirm: screen.getByLabelText("Confirm password"),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
  mockAuth.configured = true;
  mockAuth.callbackError = null;
});

describe("Login", () => {
  it("shows four fields in signup mode and blocks empty submit", () => {
    render(<Login />);
    fireEvent.click(screen.getByRole("button", { name: "Need an account? Sign up" }));
    const fields = signupFields();
    expect(fields.username).toBeDefined();
    expect(fields.confirm).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByText("Choose a username.")).toBeDefined();
    expect(screen.getByText("Enter your email address.")).toBeDefined();
    expect(screen.getByText("Choose a password.")).toBeDefined();
    expect(screen.getByText("Repeat your password.")).toBeDefined();
    expect(mockAuth.signUp).not.toHaveBeenCalled();
  });

  it("rejects a weak username and mismatched passwords", () => {
    render(<Login />);
    fireEvent.click(screen.getByRole("button", { name: "Need an account? Sign up" }));
    const fields = signupFields();
    fireEvent.change(fields.username, { target: { value: "ab" } });
    fireEvent.change(fields.email, { target: { value: "ada@example.com" } });
    fireEvent.change(fields.password, { target: { value: "s3cur3pass" } });
    fireEvent.change(fields.confirm, { target: { value: "different1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(
      screen.getByText("Username must be at least 3 characters.")
    ).toBeDefined();
    expect(screen.getByText("Passwords do not match.")).toBeDefined();
    expect(mockAuth.signUp).not.toHaveBeenCalled();
  });

  it("shows the verify-pending screen after signup with confirmation", async () => {
    mockAuth.signUp.mockResolvedValue({ error: null, needsConfirmation: true });
    render(<Login />);
    fireEvent.click(screen.getByRole("button", { name: "Need an account? Sign up" }));
    const fields = signupFields();
    fireEvent.change(fields.username, { target: { value: "ada_debugs" } });
    fireEvent.change(fields.email, { target: { value: "ada@example.com" } });
    fireEvent.change(fields.password, { target: { value: "s3cur3pass" } });
    fireEvent.change(fields.confirm, { target: { value: "s3cur3pass" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(mockAuth.signUp).toHaveBeenCalledWith(
      "ada@example.com",
      "s3cur3pass",
      "ada_debugs"
    );
    expect(await screen.findByText("Check your inbox")).toBeDefined();
    expect(screen.getByText("ada@example.com")).toBeDefined();
    // Secrets are cleared from state; the form is gone with them.
    expect(screen.queryByLabelText("Confirm password")).toBeNull();
    // Resend starts in cooldown.
    const cooldownButton = screen.getByRole("button", {
      name: /Resend email \(\d+s\)/,
    }) as HTMLButtonElement;
    expect(cooldownButton.disabled).toBe(true);
  });

  it("resend cooldown expires and success is reported", async () => {
    mockAuth.signUp.mockResolvedValue({ error: null, needsConfirmation: true });
    mockAuth.resendConfirmation.mockResolvedValue({ error: null });
    vi.useFakeTimers();
    try {
      render(<Login />);
      fireEvent.click(
        screen.getByRole("button", { name: "Need an account? Sign up" })
      );
      const fields = signupFields();
      fireEvent.change(fields.username, { target: { value: "ada_debugs" } });
      fireEvent.change(fields.email, { target: { value: "ada@example.com" } });
      fireEvent.change(fields.password, { target: { value: "s3cur3pass" } });
      fireEvent.change(fields.confirm, { target: { value: "s3cur3pass" } });
      fireEvent.click(screen.getByRole("button", { name: "Create account" }));
      // Drain the signup promise chain first so the pending screen and its
      // cooldown interval exist before virtual time advances.
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getByText("Check your inbox")).toBeDefined();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(61_000);
      });

      fireEvent.click(
        screen.getByRole("button", { name: "Resend confirmation email" })
      );
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(mockAuth.resendConfirmation).toHaveBeenCalledWith(
        "ada@example.com"
      );
      expect(
        screen.getByText("Confirmation email sent to ada@example.com.")
      ).toBeDefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports resend failures honestly", async () => {
    mockAuth.signUp.mockResolvedValue({ error: null, needsConfirmation: true });
    mockAuth.resendConfirmation.mockResolvedValue({
      error: "Email rate limit exceeded",
    });
    vi.useFakeTimers();
    try {
      render(<Login />);
      fireEvent.click(
        screen.getByRole("button", { name: "Need an account? Sign up" })
      );
      const fields = signupFields();
      fireEvent.change(fields.username, { target: { value: "ada_debugs" } });
      fireEvent.change(fields.email, { target: { value: "ada@example.com" } });
      fireEvent.change(fields.password, { target: { value: "s3cur3pass" } });
      fireEvent.change(fields.confirm, { target: { value: "s3cur3pass" } });
      fireEvent.click(screen.getByRole("button", { name: "Create account" }));
      // Drain the signup promise chain first so the pending screen and its
      // cooldown interval exist before virtual time advances.
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getByText("Check your inbox")).toBeDefined();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(61_000);
      });

      fireEvent.click(
        screen.getByRole("button", { name: "Resend confirmation email" })
      );
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getByText("Resend failed")).toBeDefined();
      expect(screen.getByText("Email rate limit exceeded")).toBeDefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("maps bad credentials to a friendly login message", async () => {
    mockAuth.signIn.mockResolvedValue({
      error: "Invalid login credentials",
      needsConfirmation: false,
    });
    render(<Login />);
    fireEvent.change(screen.getByLabelText("Email address"), {
      target: { value: "ada@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrongpass1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Incorrect email or password. Try again.")).toBeDefined();
    expect(
      screen.queryByRole("button", { name: "Resend confirmation email" })
    ).toBeNull();
  });

  it("offers resend for unconfirmed login", async () => {
    mockAuth.signIn.mockResolvedValue({
      error: "Email not confirmed",
      needsConfirmation: false,
    });
    mockAuth.resendConfirmation.mockResolvedValue({ error: null });
    render(<Login />);
    fireEvent.change(screen.getByLabelText("Email address"), {
      target: { value: "ada@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "s3cur3pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(/not confirmed yet/)).toBeDefined();
    fireEvent.click(
      screen.getByRole("button", { name: "Resend confirmation email" })
    );
    expect(mockAuth.resendConfirmation).toHaveBeenCalledWith("ada@example.com");
  });

  it("toggles password visibility", () => {
    render(<Login />);
    const input = screen.getByLabelText("Password");
    expect(input.getAttribute("type")).toBe("password");
    fireEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(input.getAttribute("type")).toBe("text");
  });

  it("GitHub button uses the mark asset and reports failures", async () => {
    mockAuth.signInWithGitHub.mockResolvedValue({
      error: "OAuth provider is not enabled",
      needsConfirmation: false,
    });
    render(<Login />);
    const button = screen.getByRole("button", { name: "Continue with GitHub" });
    const img = button.querySelector("img");
    // Vite inlines the small SVG: the data URI must carry the real mark path.
    expect(img?.getAttribute("src")).toContain("data:image/svg+xml");
    expect(img?.getAttribute("src")).toContain("M12");
    fireEvent.click(button);
    expect(mockAuth.signInWithGitHub).toHaveBeenCalled();
    expect(
      await screen.findByText("OAuth provider is not enabled")
    ).toBeDefined();
  });

  it("forgot-password flow requests a reset link", async () => {
    mockAuth.requestPasswordReset.mockResolvedValue({ error: null });
    render(<Login />);
    fireEvent.click(screen.getByRole("button", { name: "Forgot password?" }));
    expect(screen.getByText("Reset your password")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Email address"), {
      target: { value: "ada@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));

    expect(mockAuth.requestPasswordReset).toHaveBeenCalledWith(
      "ada@example.com"
    );
    expect(await screen.findByText(/a reset link is on its way/)).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: /Back to sign in/ }));
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDefined();
  });

  it("shows expired-link callback errors", () => {
    mockAuth.callbackError =
      "This link has expired. Request a fresh one below and try again.";
    render(<Login />);
    expect(screen.getByText("That link did not work")).toBeDefined();
    expect(screen.getByText(/has expired/)).toBeDefined();
  });

  it("shows a setup hint when Supabase is not configured", () => {
    mockAuth.configured = false;
    render(<Login />);
    expect(
      screen.getByText("Authentication is not configured")
    ).toBeDefined();
  });
});

describe("ResetPasswordForm", () => {
  it("validates and confirms a new password", async () => {
    mockAuth.updatePassword.mockResolvedValue({ error: null });
    render(<ResetPasswordForm />);
    fireEvent.click(screen.getByRole("button", { name: "Set new password" }));
    expect(screen.getByText("Choose a password.")).toBeDefined();
    expect(mockAuth.updatePassword).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "n3wpass12" },
    });
    fireEvent.change(screen.getByLabelText("Confirm new password"), {
      target: { value: "n3wpass12" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Set new password" }));
    expect(mockAuth.updatePassword).toHaveBeenCalledWith("n3wpass12");
    expect(await screen.findByText("Password updated")).toBeDefined();
  });

  it("rejects mismatched confirmation", () => {
    render(<ResetPasswordForm />);
    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "n3wpass12" },
    });
    fireEvent.change(screen.getByLabelText("Confirm new password"), {
      target: { value: "otherpass1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Set new password" }));
    expect(screen.getByText("Passwords do not match.")).toBeDefined();
    expect(mockAuth.updatePassword).not.toHaveBeenCalled();
  });
});
