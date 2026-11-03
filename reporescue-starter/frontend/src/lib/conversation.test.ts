import { describe, expect, it, vi } from "vitest";
import { apiJson } from "./api";
import { getMessages, postMessage } from "./conversation";

vi.mock("./api", () => ({ apiJson: vi.fn() }));

const mocked = vi.mocked(apiJson);

describe("conversation client", () => {
  it("fetches bounded history without creating", async () => {
    mocked.mockResolvedValueOnce({ session_id: null, messages: [] });
    const result = await getMessages("inv-1", 25);
    expect(mocked).toHaveBeenCalledWith(
      "/api/investigations/inv-1/messages?limit=25",
      { method: "GET" }
    );
    expect(result).toEqual({ session_id: null, messages: [] });
  });

  it("defaults the history limit", async () => {
    mocked.mockResolvedValueOnce({ session_id: "s1", messages: [] });
    await getMessages("inv-1");
    expect(mocked).toHaveBeenCalledWith(
      "/api/investigations/inv-1/messages?limit=50",
      { method: "GET" }
    );
  });

  it("posts a question and returns the persisted turn", async () => {
    const answer = {
      session_id: "s1",
      handled_as: "answer",
      detection_reason: "question framing",
      user_message: { id: "m1", role: "user", content: "Why?", created_at: null },
      assistant_message: { id: "m2", role: "assistant", content: "Because.", created_at: null },
      citations_verified: true,
      unsupported_references: [],
    };
    mocked.mockResolvedValueOnce(answer);
    const result = await postMessage("inv-1", "Why?");
    expect(mocked).toHaveBeenCalledWith("/api/investigations/inv-1/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: "Why?" }),
    });
    expect(result).toEqual(answer);
  });
});
