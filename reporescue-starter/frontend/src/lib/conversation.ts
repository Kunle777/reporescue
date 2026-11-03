import { apiJson } from "./api";
import type { ConversationAnswer, ConversationHistory } from "./types";

const JSON_HEADERS = { "Content-Type": "application/json" };

/**
 * GET /api/investigations/{id}/messages — bounded history, oldest first.
 * Never creates a session. 404 when the investigation is missing, foreign,
 * or sync-only; no terminal-state restriction on reads.
 */
export function getMessages(
  investigationId: string,
  limit = 50
): Promise<ConversationHistory> {
  const params = new URLSearchParams({ limit: String(limit) });
  return apiJson<ConversationHistory>(
    `/api/investigations/${investigationId}/messages?${params.toString()}`,
    { method: "GET" }
  );
}

/**
 * POST /api/investigations/{id}/messages — ask one follow-up question.
 * Read-only Q&A over persisted evidence: 201 with the persisted turn,
 * 409 while the investigation is still running, 422 on empty content,
 * 429/502/500 for provider failures. ApiError carries status + detail.
 */
export function postMessage(
  investigationId: string,
  content: string
): Promise<ConversationAnswer> {
  return apiJson<ConversationAnswer>(
    `/api/investigations/${investigationId}/messages`,
    {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ content }),
    }
  );
}
