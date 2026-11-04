import { describe, expect, it } from "vitest";
import {
  confirmPasswordError,
  emailError,
  normalizeUsername,
  passwordError,
  usernameError,
} from "./auth-validation";

describe("normalizeUsername", () => {
  it.each([
    ["alice", "alice"],
    ["Alice_01", "alice_01"],
    ["  bob  ", "bob"],
    ["A12", "a12"],
  ])("normalizes %p to %p", (raw, expected) => {
    expect(normalizeUsername(raw)).toBe(expected);
  });

  it.each([[null], [undefined], [42], [["alice"]], [{}]])(
    "returns empty string for %p",
    (raw) => {
      expect(normalizeUsername(raw)).toBe("");
    }
  );
});

describe("usernameError", () => {
  it.each([["alice"], ["alice_01"], ["a12"], ["x".repeat(30)]])(
    "accepts %p",
    (raw) => {
      expect(usernameError(raw)).toBeNull();
    }
  );

  it.each([
    ["", "Choose a username."],
    ["  ", "Choose a username."],
    ["ab", "Username must be at least 3 characters."],
    ["x".repeat(31), "Username must be at most 30 characters."],
    ["1abc", "Use a letter first, then lowercase letters, digits or underscores."],
    ["a-b", "Use a letter first, then lowercase letters, digits or underscores."],
    ["a b", "Use a letter first, then lowercase letters, digits or underscores."],
    ["a.b", "Use a letter first, then lowercase letters, digits or underscores."],
    ["admin", "That username is reserved. Pick another one."],
    ["ROOT", "That username is reserved. Pick another one."],
    ["support", "That username is reserved. Pick another one."],
    ["reporescue", "That username is reserved. Pick another one."],
    ["null", "That username is reserved. Pick another one."],
  ])("rejects %p", (raw, message) => {
    expect(usernameError(raw)).toBe(message);
  });
});

describe("emailError", () => {
  it("accepts a normal address", () => {
    expect(emailError("ada@example.com")).toBeNull();
  });

  it.each([
    ["", "Enter your email address."],
    ["   ", "Enter your email address."],
    ["not-an-email", "Enter a valid email address."],
    ["a@b", "Enter a valid email address."],
    ["a b@c.co", "Enter a valid email address."],
  ])("rejects %p", (raw, message) => {
    expect(emailError(raw)).toBe(message);
  });
});

describe("passwordError", () => {
  it("accepts a strong password", () => {
    expect(passwordError("s3cur3pass")).toBeNull();
  });

  it.each([
    ["", "Choose a password."],
    ["Ab1", "Password must be at least 8 characters."],
    ["abcdefgh", "Password needs at least one letter and one number."],
    ["12345678", "Password needs at least one letter and one number."],
  ])("rejects %p", (raw, message) => {
    expect(passwordError(raw)).toBe(message);
  });
});

describe("confirmPasswordError", () => {
  it("accepts a match", () => {
    expect(confirmPasswordError("s3cur3pass", "s3cur3pass")).toBeNull();
  });

  it("rejects a mismatch", () => {
    expect(confirmPasswordError("s3cur3pass", "otherpass1")).toBe(
      "Passwords do not match."
    );
  });

  it("rejects an empty confirmation", () => {
    expect(confirmPasswordError("s3cur3pass", "")).toBe(
      "Repeat your password."
    );
  });
});
