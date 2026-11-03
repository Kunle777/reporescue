import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ApiError } from "../lib/api";
import ConversationPanel from "./ConversationPanel";
import type { ConversationAnswer } from "../lib/types";

const mockConversation = vi.hoisted(() => ({ getMessages: vi.fn(), postMessage: vi.fn() }));
vi.mock("../lib/conversation", () => mockConversation);

function renderPanel(props: Record<string, unknown> = {}) {
  const handlers = { onUnauthorized: vi.fn() };
  render(
    <ConversationPanel
      investigationId="i1"
      repoName="acme/calc"
      mode="debug"
      active={false}
      onUnauthorized={handlers.onUnauthorized}
      {...props}
    />
  );
  return handlers;
}

function answer(overrides: Partial<ConversationAnswer> = {}): ConversationAnswer {
  return {
    session_id: "s1",
    handled_as: "answer",
    detection_reason: "question framing",
    user_message: { id: "m1", role: "user", content: "Why?", created_at: null },
    assistant_message: { id: "m2", role: "assistant", content: "Because.", created_at: null },
    citations_verified: true,
    grounding_status: "verified",
    unsupported_references: [],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ConversationPanel history", () => {
  it("grounds the panel in the selected investigation", async () => {
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    renderPanel();
    expect(
      await screen.findByText(/Questions about acme\/calc \(debug\) only/)
    ).toBeDefined();
    expect(mockConversation.getMessages).toHaveBeenCalledWith("i1");
  });

  it("shows a loading note while history loads", () => {
    mockConversation.getMessages.mockReturnValueOnce(new Promise(() => {}));
    renderPanel();
    expect(screen.getByText("Loading conversation…")).toBeDefined();
  });

  it("renders persisted history with grounding status", async () => {
    mockConversation.getMessages.mockResolvedValueOnce({
      session_id: "s1",
      messages: [
        { id: "m1", role: "user", content: "Why?", created_at: null },
        { id: "m2", role: "assistant", content: "Because.", created_at: null, grounding_status: "unverified" },
      ],
    });
    renderPanel();
    expect(await screen.findByText("Why?")).toBeDefined();
    expect(screen.getByText("Because.")).toBeDefined();
    expect(screen.getByText("YOU")).toBeDefined();
    expect(screen.getByText(/REPORESCUE.*UNVERIFIED/)).toBeDefined();
    expect(screen.getByText(/not fully verified/)).toBeDefined();
    expect(screen.queryByText(/GUIDANCE/)).toBeNull();
  });

  it("shows an empty state before the first question", async () => {
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    renderPanel();
    expect(await screen.findByText("No questions yet")).toBeDefined();
  });

  it("shows load errors with a retry", async () => {
    mockConversation.getMessages.mockRejectedValueOnce(new Error("boom"));
    renderPanel();
    expect(await screen.findByText("Could not load conversation")).toBeDefined();
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    fireEvent.click(screen.getByText("Retry"));
    expect(await screen.findByText("No questions yet")).toBeDefined();
    expect(mockConversation.getMessages).toHaveBeenCalledTimes(2);
  });

  it("maps a 401 on load to sign-out", async () => {
    mockConversation.getMessages.mockRejectedValueOnce(new ApiError(401, "expired"));
    const handlers = renderPanel();
    await waitFor(() => expect(handlers.onUnauthorized).toHaveBeenCalledTimes(1));
  });
});

describe("ConversationPanel questions", () => {
  it("posts a question and appends the persisted turn", async () => {
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    mockConversation.postMessage.mockResolvedValueOnce(answer());
    renderPanel();
    expect(await screen.findByText("No questions yet")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Ask about this investigation"), {
      target: { value: "Why?" },
    });
    fireEvent.click(screen.getByText("Ask"));
    expect(mockConversation.postMessage).toHaveBeenCalledWith("i1", "Why?");
    expect(await screen.findByText("Because.")).toBeDefined();
    expect(screen.getByText(/EVIDENCE VERIFIED/)).toBeDefined();
    expect((screen.getByLabelText("Ask about this investigation") as HTMLInputElement).value).toBe("");
  });

  it("rejects blank questions without calling the API", async () => {
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    renderPanel();
    expect(await screen.findByText("No questions yet")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Ask about this investigation"), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText("Type a question first.")).toBeDefined();
    expect(mockConversation.postMessage).not.toHaveBeenCalled();
  });

  it("disables the composer while the investigation is running", async () => {
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    renderPanel({ active: true });
    expect(await screen.findByText(/Conversation unlocks when the investigation completes/)).toBeDefined();
    expect(screen.queryByLabelText("Ask about this investigation")).toBeNull();
    expect(screen.queryByText("Ask")).toBeNull();
  });

  it("explains a 409 race as still running", async () => {
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    mockConversation.postMessage.mockRejectedValueOnce(new ApiError(409, "running"));
    renderPanel();
    expect(await screen.findByText("No questions yet")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Ask about this investigation"), {
      target: { value: "Why?" },
    });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText(/still running/)).toBeDefined();
  });

  it("explains rate limiting and provider failures honestly", async () => {
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    mockConversation.postMessage.mockRejectedValueOnce(new ApiError(429, "slow down"));
    renderPanel();
    expect(await screen.findByText("No questions yet")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Ask about this investigation"), {
      target: { value: "Why?" },
    });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText(/Too many questions/)).toBeDefined();

    mockConversation.postMessage.mockRejectedValueOnce(new ApiError(502, "provider down"));
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText("provider down")).toBeDefined();
  });

  it("renders guidance answers distinctly", async () => {
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    mockConversation.postMessage.mockResolvedValueOnce(
      answer({
        handled_as: "guidance",
        grounding_status: "non_factual",
        assistant_message: { id: "m2", role: "assistant", content: "I cannot apply changes.", created_at: null },
        citations_verified: false,
      })
    );
    renderPanel();
    expect(await screen.findByText("No questions yet")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Ask about this investigation"), {
      target: { value: "Apply it." },
    });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText("I cannot apply changes.")).toBeDefined();
    const bubble = screen.getByText("I cannot apply changes.").closest(".msg");
    expect(bubble?.className).toContain("guidance");
    expect(screen.getByText(/GUIDANCE/)).toBeDefined();
    expect(screen.queryByText(/GROUNDED/)).toBeNull();
  });

  it("flags unsupported references as unconfirmed", async () => {
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    mockConversation.postMessage.mockResolvedValueOnce(
      answer({
        unsupported_references: [{ reference: "pricing/coupons.py:12", reason: "not in evidence" }],
      })
    );
    renderPanel();
    expect(await screen.findByText("No questions yet")).toBeDefined();
    fireEvent.change(screen.getByLabelText("Ask about this investigation"), {
      target: { value: "What about coupons?" },
    });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText(/1 unverified reference/)).toBeDefined();
    expect(screen.getByText("pricing/coupons.py:12")).toBeDefined();
    expect(screen.getByText(/not in evidence/)).toBeDefined();
  });

  it.each([
    ["verified", "EVIDENCE VERIFIED"],
    ["unverified", "UNVERIFIED"],
    ["citation_issue", "CITATION ISSUE"],
    ["non_factual", "NON-FACTUAL"],
  ] as const)("renders grounding status %s accurately", async (status, label) => {
    mockConversation.getMessages.mockResolvedValueOnce({ session_id: null, messages: [] });
    mockConversation.postMessage.mockResolvedValueOnce(answer({ grounding_status: status }));
    renderPanel();
    await screen.findByText("No questions yet");
    fireEvent.change(screen.getByLabelText("Ask about this investigation"), { target: { value: "Why?" } });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText(new RegExp(label))).toBeDefined();
  });
});
