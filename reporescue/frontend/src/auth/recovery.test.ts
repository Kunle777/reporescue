import { describe, expect, it } from "vitest";
import {
  accessTokenIndicatesRecovery,
  callbackErrorMessage,
  cleanCallbackUrl,
  parseAuthCallback,
  signInErrorMessage,
} from "./recovery";

function tokenWithPayload(payload: unknown): string {
  const encode = (value: unknown) =>
    btoa(JSON.stringify(value))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  return `${encode({ alg: "ES256", typ: "JWT" })}.${encode(payload)}.sig`;
}

describe("parseAuthCallback", () => {
  it("finds a PKCE code", () => {
    expect(parseAuthCallback("http://x/?code=abc123")).toEqual({
      kind: "code",
      code: "abc123",
    });
  });

  it("finds a token_hash callback", () => {
    expect(
      parseAuthCallback("http://x/?token_hash=h&type=recovery")
    ).toEqual({ kind: "token_hash", tokenHash: "h", type: "recovery" });
  });

  it.each([
    ["http://x/"],
    ["http://x/?code="],
    ["http://x/?token_hash=h&type=bogus"],
    ["http://x/?token_hash=&type=recovery"],
    ["not a url at all %%%"],
  ])("returns none for %p", (href) => {
    expect(parseAuthCallback(href)).toEqual({ kind: "none" });
  });
});

describe("cleanCallbackUrl", () => {
  it("strips callback params", () => {
    expect(cleanCallbackUrl("http://x/app?code=a&b=c#top")).toBe(
      "/app?b=c#top"
    );
  });

  it("falls back to root for garbage", () => {
    expect(cleanCallbackUrl("%%%")).toBe("/");
  });
});

describe("accessTokenIndicatesRecovery", () => {
  it("detects a recovery amr method", () => {
    const token = tokenWithPayload({
      sub: "u",
      amr: [{ method: "recovery", timestamp: 1 }],
    });
    expect(accessTokenIndicatesRecovery(token)).toBe(true);
  });

  it("ignores password sessions", () => {
    const token = tokenWithPayload({
      sub: "u",
      amr: [{ method: "password", timestamp: 1 }],
    });
    expect(accessTokenIndicatesRecovery(token)).toBe(false);
  });

  it.each([
    [tokenWithPayload({ sub: "u" })],
    [tokenWithPayload({ sub: "u", amr: "recovery" })],
    ["not-a-token"],
    [""],
  ])("returns false for %p", (token) => {
    expect(accessTokenIndicatesRecovery(token)).toBe(false);
  });
});

describe("callbackErrorMessage", () => {
  it("maps expiry honestly", () => {
    expect(callbackErrorMessage("Token expired long ago")).toContain(
      "expired"
    );
  });

  it("maps invalid/used links", () => {
    expect(callbackErrorMessage("invalid token")).toContain(
      "invalid or was already used"
    );
  });

  it("passes unknown errors through", () => {
    expect(callbackErrorMessage("weird upstream outage")).toBe(
      "weird upstream outage"
    );
  });
});

describe("signInErrorMessage", () => {
  it("flags unconfirmed email", () => {
    const mapped = signInErrorMessage("Email not confirmed");
    expect(mapped.emailUnconfirmed).toBe(true);
    expect(mapped.message).toContain("not confirmed");
  });

  it("simplifies bad credentials", () => {
    const mapped = signInErrorMessage("Invalid login credentials");
    expect(mapped.emailUnconfirmed).toBe(false);
    expect(mapped.message).toBe("Incorrect email or password. Try again.");
  });

  it("passes unknown errors through", () => {
    const mapped = signInErrorMessage("something odd");
    expect(mapped).toEqual({ message: "something odd", emailUnconfirmed: false });
  });
});
