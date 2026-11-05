import { apiJson } from "./api";
import type { InvestigationMode } from "./types";

export type Classification =
  | {
      selected_mode: InvestigationMode;
      rationale: string;
      confidence: string;
      signals: Record<string, string[]>;
      source: string;
    }
  | {
      selected_mode: null;
      reason: "needs_objective" | "needs_clarification";
      question?: string;
      rationale: string;
      confidence: string;
      signals: Record<string, string[]>;
      suggested_mode: InvestigationMode | null;
      source: string;
    };

/**
 * Classify a General objective without creating a job. The backend
 * routes deterministically first and consults the model only for
 * unclear objectives; a vague objective returns needs_clarification
 * with a follow-up question instead of a route.
 */
export async function classifyObjective(
  message: string
): Promise<Classification> {
  return apiJson<Classification>("/api/general/classify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
}
