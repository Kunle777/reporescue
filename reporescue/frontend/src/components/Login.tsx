import { useEffect, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Eye,
  EyeOff,
  LoaderCircle,
  MailCheck,
} from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { signInErrorMessage } from "../auth/recovery";
import {
  confirmPasswordError,
  emailError,
  passwordError,
  usernameError,
} from "../lib/auth-validation";
import githubMark from "../assets/github-mark.svg";

type Mode = "signin" | "signup";
type Screen = "form" | "pending" | "forgot";

/** Seconds between confirmation-email resends. */
const RESEND_COOLDOWN_SECONDS = 60;

function NotConfigured() {
  return (
    <div className="banner banner-error">
      <AlertTriangle size={17} />
      <div>
        <strong>Authentication is not configured</strong>
        <p>
          Copy frontend/.env.example to .env.local and fill in VITE_SUPABASE_URL
          and VITE_SUPABASE_ANON_KEY.
        </p>
      </div>
    </div>
  );
}

/** Email + GitHub auth: sign-in, 4-field signup, verify-pending, recovery. */
export default function Login() {
  const {
    signUp,
    signIn,
    signInWithGitHub,
    resendConfirmation,
    requestPasswordReset,
    configured,
    callbackError,
    clearCallbackError,
  } = useAuth();

  const [mode, setMode] = useState<Mode>("signin");
  const [screen, setScreen] = useState<Screen>("form");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [githubBusy, setGithubBusy] = useState(false);
  const [error, setError] = useState("");
  const [unconfirmedEmail, setUnconfirmedEmail] = useState("");

  const [pendingEmail, setPendingEmail] = useState("");
  const [resendBusy, setResendBusy] = useState(false);
  const [resendCooldown, setResendCooldown] = useState(0);
  const [resendMessage, setResendMessage] = useState("");
  const [resendFailed, setResendFailed] = useState(false);

  const [forgotBusy, setForgotBusy] = useState(false);
  const [forgotMessage, setForgotMessage] = useState("");
  const [forgotFailed, setForgotFailed] = useState(false);

  useEffect(() => {
    if (resendCooldown <= 0) return;
    const timer = setInterval(() => {
      setResendCooldown((value) => (value > 0 ? value - 1 : 0));
    }, 1000);
    return () => clearInterval(timer);
  }, [resendCooldown]);

  /** Clear secrets + notices when leaving a screen. Never persists confirm. */
  function goScreen(next: Screen) {
    setScreen(next);
    setPassword("");
    setConfirm("");
    setFieldErrors({});
    setError("");
    setUnconfirmedEmail("");
    setResendMessage("");
    setResendFailed(false);
    setForgotMessage("");
    setForgotFailed(false);
    clearCallbackError();
  }

  function switchMode(next: Mode) {
    setMode(next);
    goScreen("form");
    if (next === "signin") setUsername("");
  }

  function validateSignIn(): boolean {
    const errors: Record<string, string> = {};
    const emailInvalid = emailError(email);
    if (emailInvalid) errors.email = emailInvalid;
    if (!password) errors.password = "Enter your password.";
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  function validateSignUp(): boolean {
    const errors: Record<string, string> = {};
    const usernameInvalid = usernameError(username);
    if (usernameInvalid) errors.username = usernameInvalid;
    const emailInvalid = emailError(email);
    if (emailInvalid) errors.email = emailInvalid;
    const passwordInvalid = passwordError(password);
    if (passwordInvalid) errors.password = passwordInvalid;
    const confirmInvalid = confirmPasswordError(password, confirm);
    if (confirmInvalid) errors.confirm = confirmInvalid;
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setUnconfirmedEmail("");
    clearCallbackError();
    const valid = mode === "signin" ? validateSignIn() : validateSignUp();
    if (!valid) return;
    setBusy(true);
    try {
      if (mode === "signin") {
        const result = await signIn(email.trim(), password);
        if (result.error) {
          const mapped = signInErrorMessage(result.error);
          setError(mapped.message);
          if (mapped.emailUnconfirmed) setUnconfirmedEmail(email.trim());
        }
        return;
      }
      const result = await signUp(email.trim(), password, username);
      // Drop secrets from state as soon as the request completes.
      setPassword("");
      setConfirm("");
      if (result.error) {
        setError(result.error);
        return;
      }
      if (result.needsConfirmation) {
        setPendingEmail(email.trim());
        setResendCooldown(RESEND_COOLDOWN_SECONDS);
        setScreen("pending");
      }
      // Without email confirmation Supabase signs in immediately and the
      // app switches views via the session; nothing left to show here.
    } finally {
      setBusy(false);
    }
  }

  async function github() {
    setGithubBusy(true);
    setError("");
    clearCallbackError();
    try {
      const result = await signInWithGitHub();
      if (result.error) setError(result.error);
    } finally {
      setGithubBusy(false);
    }
  }

  /** Resend used by both the pending screen and unconfirmed-login hint. */
  async function resend(toEmail: string) {
    if (resendBusy || resendCooldown > 0) return;
    setResendBusy(true);
    setResendMessage("");
    setResendFailed(false);
    try {
      const result = await resendConfirmation(toEmail);
      if (result.error) {
        setResendFailed(true);
        // Honest failure: Supabase owns delivery, including rate limits.
        setResendMessage(result.error);
      } else {
        setResendMessage(`Confirmation email sent to ${toEmail}.`);
        setResendCooldown(RESEND_COOLDOWN_SECONDS);
      }
    } finally {
      setResendBusy(false);
    }
  }

  async function forgot(event: FormEvent) {
    event.preventDefault();
    setForgotMessage("");
    setForgotFailed(false);
    const emailInvalid = emailError(email);
    if (emailInvalid) {
      setFieldErrors({ email: emailInvalid });
      return;
    }
    setFieldErrors({});
    setForgotBusy(true);
    try {
      const result = await requestPasswordReset(email.trim());
      if (result.error) {
        setForgotFailed(true);
        setForgotMessage(result.error);
      } else {
        setForgotMessage(
          `If ${email.trim()} is registered, a reset link is on its way. ` +
            "Check your inbox (and spam folder)."
        );
      }
    } finally {
      setForgotBusy(false);
    }
  }

  if (!configured) return <NotConfigured />;

  if (screen === "pending") {
    return (
      <div className="verify-panel">
        <span className="verify-icon" aria-hidden="true">
          <MailCheck size={22} />
        </span>
        <h3 className="verify-title">Check your inbox</h3>
        <p className="hint">
          We sent a confirmation link to <strong>{pendingEmail}</strong>. Open
          it to verify your account, then sign in. The link expires after a
          while — request a fresh one below if needed.
        </p>
        {resendMessage && (
          <div
            className={`banner ${resendFailed ? "banner-error" : "banner-success"}`}
          >
            {resendFailed ? (
              <AlertTriangle size={17} />
            ) : (
              <CheckCircle2 size={17} />
            )}
            <div>
              <strong>{resendFailed ? "Resend failed" : "Email sent"}</strong>
              <p>{resendMessage}</p>
            </div>
          </div>
        )}
        <div className="resend-row">
          <button
            className="btn btn-secondary btn-sm"
            type="button"
            disabled={resendBusy || resendCooldown > 0}
            onClick={() => void resend(pendingEmail)}
          >
            {resendBusy && <LoaderCircle className="spin" size={14} />}
            {resendCooldown > 0
              ? `Resend email (${resendCooldown}s)`
              : "Resend confirmation email"}
          </button>
        </div>
        <button
          className="link-button"
          type="button"
          onClick={() => switchMode("signin")}
        >
          <span className="link-inline">
            <ArrowLeft size={14} /> Back to sign in
          </span>
        </button>
      </div>
    );
  }

  if (screen === "forgot") {
    return (
      <div>
        <form onSubmit={(event) => void forgot(event)} className="form-stack">
          <div>
            <h3 className="verify-title">Reset your password</h3>
            <p className="hint">
              Enter your account email and we will send a reset link.
            </p>
          </div>
          <div className="field">
            <label className="label" htmlFor="forgot-email">
              Email address
            </label>
            <input
              id="forgot-email"
              className={`input${fieldErrors.email ? " input-invalid" : ""}`}
              type="email"
              placeholder="you@example.com"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
            />
            {fieldErrors.email && (
              <span className="field-error">{fieldErrors.email}</span>
            )}
          </div>
          <button
            className="btn btn-primary btn-block"
            type="submit"
            disabled={forgotBusy}
          >
            {forgotBusy && <LoaderCircle className="spin" size={16} />}
            Send reset link
          </button>
        </form>
        {forgotMessage && (
          <div
            className={`banner ${forgotFailed ? "banner-error" : "banner-success"}`}
          >
            {forgotFailed ? (
              <AlertTriangle size={17} />
            ) : (
              <CheckCircle2 size={17} />
            )}
            <div>
              <strong>{forgotFailed ? "Reset failed" : "Check your inbox"}</strong>
              <p>{forgotMessage}</p>
            </div>
          </div>
        )}
        <div className="auth-alt">
          <button
            className="link-button"
            type="button"
            onClick={() => switchMode("signin")}
          >
            <span className="link-inline">
              <ArrowLeft size={14} /> Back to sign in
            </span>
          </button>
        </div>
      </div>
    );
  }

  return (
    <div>
      {callbackError && (
        <div className="banner banner-error">
          <AlertTriangle size={17} />
          <div>
            <strong>That link did not work</strong>
            <p>{callbackError}</p>
          </div>
        </div>
      )}
      <form onSubmit={(event) => void submit(event)} className="form-stack">
        {mode === "signup" && (
          <div className="field">
            <label className="label" htmlFor="login-username">
              Username
            </label>
            <input
              id="login-username"
              className={`input${fieldErrors.username ? " input-invalid" : ""}`}
              type="text"
              placeholder="e.g. ada_debugs"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              maxLength={30}
            />
            {fieldErrors.username ? (
              <span className="field-error">{fieldErrors.username}</span>
            ) : (
              <span className="hint">
                3–30 characters: a letter first, then letters, digits or _ .
              </span>
            )}
          </div>
        )}
        <div className="field">
          <label className="label" htmlFor="login-email">
            Email address
          </label>
          <input
            id="login-email"
            className={`input${fieldErrors.email ? " input-invalid" : ""}`}
            type="email"
            placeholder="you@example.com"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            autoComplete="email"
          />
          {fieldErrors.email && (
            <span className="field-error">{fieldErrors.email}</span>
          )}
        </div>
        <div className="field">
          <label className="label" htmlFor="login-password">
            Password
          </label>
          <div className="password-wrap">
            <input
              id="login-password"
              className={`input${fieldErrors.password ? " input-invalid" : ""}`}
              type={showPassword ? "text" : "password"}
              placeholder={
                mode === "signin" ? "Your password" : "8+ characters"
              }
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={
                mode === "signin" ? "current-password" : "new-password"
              }
            />
            <button
              type="button"
              className="password-toggle"
              aria-label={showPassword ? "Hide password" : "Show password"}
              onClick={() => setShowPassword((value) => !value)}
            >
              {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
          {fieldErrors.password && (
            <span className="field-error">{fieldErrors.password}</span>
          )}
        </div>
        {mode === "signup" && (
          <div className="field">
            <label className="label" htmlFor="login-confirm">
              Confirm password
            </label>
            <input
              id="login-confirm"
              className={`input${fieldErrors.confirm ? " input-invalid" : ""}`}
              type={showPassword ? "text" : "password"}
              placeholder="Repeat your password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              autoComplete="new-password"
            />
            {fieldErrors.confirm && (
              <span className="field-error">{fieldErrors.confirm}</span>
            )}
          </div>
        )}
        <button className="btn btn-primary btn-block" type="submit" disabled={busy}>
          {busy ? (
            <LoaderCircle className="spin" size={16} />
          ) : mode === "signin" ? (
            "Sign in"
          ) : (
            "Create account"
          )}
        </button>
      </form>
      <div className="auth-alt">
        <button
          className="btn btn-github btn-block"
          type="button"
          onClick={() => void github()}
          disabled={githubBusy || busy}
        >
          {githubBusy ? (
            <LoaderCircle className="spin" size={16} />
          ) : (
            <img src={githubMark} alt="" width={18} height={18} />
          )}
          Continue with GitHub
        </button>
        <div className="auth-links">
          <button
            className="link-button"
            type="button"
            disabled={busy}
            onClick={() => switchMode(mode === "signin" ? "signup" : "signin")}
          >
            {mode === "signin"
              ? "Need an account? Sign up"
              : "Have an account? Sign in"}
          </button>
          {mode === "signin" && (
            <button
              className="link-button"
              type="button"
              disabled={busy}
              onClick={() => goScreen("forgot")}
            >
              Forgot password?
            </button>
          )}
        </div>
      </div>
      {error && (
        <div className="banner banner-error">
          <AlertTriangle size={17} />
          <div>
            <strong>Authentication failed</strong>
            <p>{error}</p>
            {unconfirmedEmail && (
              <div className="resend-row">
                <button
                  className="btn btn-secondary btn-sm"
                  type="button"
                  disabled={resendBusy || resendCooldown > 0}
                  onClick={() => void resend(unconfirmedEmail)}
                >
                  {resendBusy && <LoaderCircle className="spin" size={14} />}
                  {resendCooldown > 0
                    ? `Resend email (${resendCooldown}s)`
                    : "Resend confirmation email"}
                </button>
              </div>
            )}
            {resendMessage && <p>{resendMessage}</p>}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * New-password form shown after a reset link lands (recovery session).
 * Rendered by App instead of the dashboard while recovery is pending.
 */
export function ResetPasswordForm() {
  const { updatePassword, signOut } = useAuth();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    const errors: Record<string, string> = {};
    const passwordInvalid = passwordError(password);
    if (passwordInvalid) errors.password = passwordInvalid;
    const confirmInvalid = confirmPasswordError(password, confirm);
    if (confirmInvalid) errors.confirm = confirmInvalid;
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;
    setBusy(true);
    try {
      const result = await updatePassword(password);
      setPassword("");
      setConfirm("");
      if (result.error) {
        setError(result.error);
      } else {
        setDone(true);
      }
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="banner banner-success">
        <CheckCircle2 size={17} />
        <div>
          <strong>Password updated</strong>
          <p>You are signed in with your new password.</p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <form onSubmit={(event) => void submit(event)} className="form-stack">
        <div className="field">
          <label className="label" htmlFor="reset-password">
            New password
          </label>
          <input
            id="reset-password"
            className={`input${fieldErrors.password ? " input-invalid" : ""}`}
            type="password"
            placeholder="8+ characters"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="new-password"
          />
          {fieldErrors.password && (
            <span className="field-error">{fieldErrors.password}</span>
          )}
        </div>
        <div className="field">
          <label className="label" htmlFor="reset-confirm">
            Confirm new password
          </label>
          <input
            id="reset-confirm"
            className={`input${fieldErrors.confirm ? " input-invalid" : ""}`}
            type="password"
            placeholder="Repeat your new password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
            autoComplete="new-password"
          />
          {fieldErrors.confirm && (
            <span className="field-error">{fieldErrors.confirm}</span>
          )}
        </div>
        <button className="btn btn-primary btn-block" type="submit" disabled={busy}>
          {busy ? <LoaderCircle className="spin" size={16} /> : "Set new password"}
        </button>
      </form>
      {error && (
        <div className="banner banner-error">
          <AlertTriangle size={17} />
          <div>
            <strong>Could not update the password</strong>
            <p>{error}</p>
          </div>
        </div>
      )}
      <div className="auth-alt">
        <button
          className="link-button"
          type="button"
          onClick={() => void signOut()}
        >
          Cancel and sign out
        </button>
      </div>
    </div>
  );
}
