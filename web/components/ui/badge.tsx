/**
 * Status chips. Every status carries an icon and a text label, never colour
 * alone (PRD FR-A11Y-005).
 */

type Tone = "neutral" | "positive" | "warning" | "critical" | "info" | "accent";

const TONES: Record<Tone, { className: string; icon: string }> = {
  neutral: { className: "bg-[var(--color-raised)] text-[var(--color-text-secondary)]", icon: "•" },
  positive: { className: "bg-[var(--color-positive-quiet)] text-[var(--color-positive)]", icon: "✓" },
  warning: { className: "bg-[var(--color-warning-quiet)] text-[var(--color-warning)]", icon: "!" },
  critical: { className: "bg-[var(--color-critical-quiet)] text-[var(--color-critical)]", icon: "✕" },
  info: { className: "bg-[var(--color-info-quiet)] text-[var(--color-info)]", icon: "i" },
  accent: { className: "bg-[var(--color-accent-quiet)] text-[var(--color-accent)]", icon: "◆" },
};

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: string }) {
  const { className, icon } = TONES[tone];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${className}`}
    >
      <span aria-hidden="true">{icon}</span>
      {children}
    </span>
  );
}

/**
 * A candidate fact and its source. Hovering reveals where it came from, which
 * is what makes grounding legible rather than a claim (PRD §12.6).
 */
export function EvidenceChip({ label, source }: { label: string; source?: string }) {
  return (
    <span
      title={source}
      className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--color-accent)] bg-[var(--color-accent-quiet)] px-2 py-0.5 text-xs text-[var(--color-accent)]"
    >
      <span aria-hidden="true">◆</span>
      {label}
      {source ? <span className="sr-only">, from {source}</span> : null}
    </span>
  );
}
