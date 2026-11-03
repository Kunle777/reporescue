import { useEffect, useState } from "react";
import { AlertTriangle, LoaderCircle } from "lucide-react";
import { ApiError } from "../lib/api";
import { getMessages, postMessage } from "../lib/conversation";
import type { ConversationAnswer, ConversationMessage, InvestigationMode } from "../lib/types";

/**
 * Investigation-scoped Q&A. Read-only by design: asking never runs code,
 * reruns tests, or changes the repository — the composer only posts
 * questions, and the backend answers from persisted evidence. History loads
 * in any job state; asking unlocks when the investigation completes
 * (the backend answers 409 while it is still running).
 */
export default function ConversationPanel({
  investigationId,
  repoName,
  mode,
  active,
  onUnauthorized,
}: {
  investigationId: string;
  repoName: string;
  mode: InvestigationMode;
  /** True while the job is still running: the composer stays disabled. */
  active: boolean;
  onUnauthorized: () => void;
}) {
  const [history, setHistory] = useState<ConversationMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [retryTick, setRetryTick] = useState(0);
  const [draft, setDraft] = useState("");
  const [posting, setPosting] = useState(false);
  const [postError, setPostError] = useState("");
  const [lastAnswer, setLastAnswer] = useState<ConversationAnswer | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    setHistory([]);
    setLastAnswer(null);
    void (async () => {
      try {
        const data = await getMessages(investigationId);
        if (!cancelled) setHistory(data.messages);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          onUnauthorized();
          return;
        }
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [investigationId, retryTick]);

  async function ask() {
    const content = draft.trim();
    if (!content) {
      setPostError("Type a question first.");
      return;
    }
    setPosting(true);
    setPostError("");
    try {
      const answer = await postMessage(investigationId, content);
      setHistory((prev) => [...prev, answer.user_message, answer.assistant_message]);
      setLastAnswer(answer);
      setDraft("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        onUnauthorized();
        return;
      }
      if (err instanceof ApiError && err.status === 409) {
        setPostError("The investigation is still running — conversation unlocks when it completes.");
        return;
      }
      if (err instanceof ApiError && err.status === 429) {
        setPostError("Too many questions at once — wait a moment and try again.");
        return;
      }
      setPostError(err instanceof Error ? err.message : "Could not reach the RepoRescue API.");
    } finally {
      setPosting(false);
    }
  }

  const freshAssistantId = lastAnswer?.assistant_message.id ?? null;

  function groundingLabel(status: ConversationMessage["grounding_status"]): string {
    switch (status) {
      case "verified": return " · EVIDENCE VERIFIED";
      case "citation_issue": return " · CITATION ISSUE";
      case "non_factual": return " · NON-FACTUAL";
      case "unverified": return " · UNVERIFIED";
      default: return " · STATUS UNKNOWN";
    }
  }

  function roleLabel(message: ConversationMessage): string {
    if (message.role === "user") return "YOU";
    if (message.role === "system") return "SYSTEM";
    return "REPORESCUE";
  }

  return (
    <div>
      <p className="hint">
        Questions about {repoName} ({mode}) only. Asking never runs code, reruns tests, or
        changes the repository.
      </p>

      {error && (
        <div className="banner banner-error">
          <AlertTriangle size={17} />
          <div>
            <strong>Could not load conversation</strong>
            <p>{error}</p>
          </div>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => setRetryTick((tick) => tick + 1)}
          >
            Retry
          </button>
        </div>
      )}

      {loading ? (
        <div className="loading-note">
          <LoaderCircle size={14} className="spin" /> Loading conversation…
        </div>
      ) : (
        history.length === 0 &&
        !error && (
          <div className="empty-state">
            <strong>No questions yet</strong>
            <p>Ask about the evidence, the diagnosis, or what the verification run proved.</p>
          </div>
        )
      )}

      {!loading && history.length > 0 && (
        <div className="chat">
          {history.map((message) => {
            const fresh = lastAnswer && message.id === freshAssistantId ? lastAnswer : null;
            const guidance = fresh?.handled_as === "guidance";
            const groundingStatus = fresh?.grounding_status ?? message.grounding_status;
            return (
              <div
                key={message.id}
                className={`msg ${message.role === "user" ? "user" : "assistant"}${guidance ? " guidance" : ""}`}
              >
                <span className="mrole">
                  {roleLabel(message)}
                  {message.role === "assistant" ? groundingLabel(groundingStatus) : ""}
                  {guidance ? " · GUIDANCE" : ""}
                </span>
                <p>{message.content}</p>
                {message.role === "assistant" && groundingStatus === "verified" && (
                  <p className="hint">Citations resolve to saved investigation evidence; citation matching does not independently prove semantic entailment.</p>
                )}
                {message.role === "assistant" && (groundingStatus === "unverified" || groundingStatus === "citation_issue" || !groundingStatus) && (
                  <p className="hint">This response is not fully verified against saved investigation evidence.</p>
                )}
                {fresh && fresh.unsupported_references.length > 0 && (
                  <div className="banner banner-warn" style={{ marginTop: "6px" }}>
                    <div>
                      <strong>
                        {fresh.unsupported_references.length} unverified reference
                        {fresh.unsupported_references.length === 1 ? "" : "s"}
                      </strong>
                      {fresh.unsupported_references.map((item) => (
                        <p key={item.reference}>
                          <span className="mono">{item.reference}</span> — {item.reason} Treated
                          as unconfirmed.
                        </p>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {postError && (
        <div className="banner banner-error">
          <AlertTriangle size={17} />
          <div>
            <strong>Could not send question</strong>
            <p>{postError}</p>
          </div>
        </div>
      )}

      {active ? (
        <p className="hint" style={{ marginTop: "12px" }}>
          Conversation unlocks when the investigation completes — questions cannot be answered
          while it is still running.
        </p>
      ) : (
        <div className="composer">
          <input
            className="input input-grow"
            placeholder="What did the verification run prove?"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            aria-label="Ask about this investigation"
            disabled={posting}
          />
          <button className="btn btn-primary" onClick={() => void ask()} disabled={posting}>
            {posting ? <LoaderCircle className="spin" size={16} /> : null}
            {posting ? "Asking…" : "Ask"}
          </button>
        </div>
      )}
    </div>
  );
}
