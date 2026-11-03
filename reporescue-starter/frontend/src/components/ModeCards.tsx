import type { InvestigationMode } from "../lib/types";

const MODES: { id: InvestigationMode; label: string; blurb: string; isDefault?: boolean }[] = [
  {
    id: "general",
    label: "General",
    blurb: "RepoRescue picks the right investigation for your question.",
    isDefault: true,
  },
  {
    id: "debug",
    label: "Debug",
    blurb: "Reproduce a failure and verify a fix with tests.",
  },
  {
    id: "explain",
    label: "Explain",
    blurb: "Understand structure and behavior. Static only.",
  },
  {
    id: "health",
    label: "Health",
    blurb: "Assess quality, security and reliability. Static only.",
  },
];

/** Mode picker cards. General is the default per the approved design. */
export default function ModeCards({
  value,
  onChange,
  disabled = false,
}: {
  value: InvestigationMode;
  onChange: (mode: InvestigationMode) => void;
  disabled?: boolean;
}) {
  return (
    <div className="mode-grid" role="radiogroup" aria-label="Investigation mode">
      {MODES.map((mode) => (
        <button
          key={mode.id}
          type="button"
          role="radio"
          aria-checked={value === mode.id}
          className={`mode-card${value === mode.id ? " selected" : ""}`}
          onClick={() => onChange(mode.id)}
          disabled={disabled}
        >
          {mode.isDefault && <span className="def">DEFAULT</span>}
          <strong>{mode.label}</strong>
          <span>{mode.blurb}</span>
        </button>
      ))}
    </div>
  );
}
