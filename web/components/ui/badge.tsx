/**
 * Status chips and evidence (Verity Design Language v2).
 *
 * Every status carries an icon and a text label, never colour alone
 * (PRD FR-A11Y-005). Icons are drawn rather than typed: a glyph like "✕"
 * inherits the text metrics and sits badly against small caps, where a sized
 * SVG stays optically centred.
 */

import type { ReactNode } from "react";

type Tone = "neutral" | "positive" | "warning" | "critical" | "info" | "accent";

const TONES: Record<Tone, string> = {
  neutral:
    "bg-[var(--color-raised)] text-[var(--color-text-secondary)] border-[var(--color-border-subtle)]",
  positive:
    "bg-[var(--color-positive-quiet)] text-[var(--color-positive)] border-[var(--color-positive-border)]",
  warning:
    "bg-[var(--color-warning-quiet)] text-[var(--color-warning)] border-[var(--color-warning-border)]",
  critical:
    "bg-[var(--color-critical-quiet)] text-[var(--color-critical)] border-[var(--color-critical-border)]",
  info: "bg-[var(--color-info-quiet)] text-[var(--color-info)] border-[var(--color-info-border)]",
  accent:
    "bg-[var(--color-accent-quiet)] text-[var(--color-accent)] border-[var(--color-accent-border)]",
};

const ICONS: Record<Tone, ReactNode> = {
  neutral: <Dot />,
  positive: <Check />,
  warning: <Alert />,
  critical: <Cross />,
  info: <Info />,
  accent: <Diamond />,
};

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${TONES[tone]}`}
    >
      <span aria-hidden="true" className="shrink-0">
        {ICONS[tone]}
      </span>
      {children}
    </span>
  );
}

/**
 * A candidate fact and where it came from. Hovering reveals the source, which
 * is what makes grounding legible rather than a claim (PRD §12.6).
 */
export function EvidenceChip({ label, source }: { label: string; source?: string }) {
  return (
    <span
      title={source}
      className="inline-flex max-w-full items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--color-accent-border)] bg-[var(--color-accent-quiet)] px-2 py-1 text-xs text-[var(--color-accent)]"
    >
      <span aria-hidden="true" className="shrink-0">
        <Diamond />
      </span>
      <span className="truncate">{label}</span>
      {source ? <span className="sr-only">, from {source}</span> : null}
    </span>
  );
}

/** A labelled figure — readiness, match, counts. Tabular so it never jitters. */
export function Stat({
  value,
  label,
  tone = "neutral",
}: {
  value: string;
  label: string;
  tone?: "neutral" | "accent";
}) {
  return (
    <div className="space-y-0.5">
      <p
        className={`numeric text-2xl ${
          tone === "accent" ? "text-[var(--color-accent)]" : "text-[var(--color-text-primary)]"
        }`}
      >
        {value}
      </p>
      <p className="text-xs text-[var(--color-text-secondary)]">{label}</p>
    </div>
  );
}

const ICON = "size-3";

function Check() {
  return (
    <svg viewBox="0 0 12 12" className={ICON} fill="none">
      <path
        d="M2.5 6.5 5 9l4.5-6"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Cross() {
  return (
    <svg viewBox="0 0 12 12" className={ICON} fill="none">
      <path
        d="m3 3 6 6M9 3l-6 6"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Alert() {
  return (
    <svg viewBox="0 0 12 12" className={ICON} fill="none">
      <path d="M6 2v4.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="6" cy="9.2" r="0.9" fill="currentColor" />
    </svg>
  );
}

function Info() {
  return (
    <svg viewBox="0 0 12 12" className={ICON} fill="none">
      <circle cx="6" cy="6" r="4.6" stroke="currentColor" strokeWidth="1.4" />
      <path d="M6 5.4v3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="6" cy="3.6" r="0.7" fill="currentColor" />
    </svg>
  );
}

function Dot() {
  return (
    <svg viewBox="0 0 12 12" className={ICON} fill="none">
      <circle cx="6" cy="6" r="2.4" fill="currentColor" />
    </svg>
  );
}

function Diamond() {
  return (
    <svg viewBox="0 0 12 12" className={ICON} fill="none">
      <path d="M6 1.6 10.4 6 6 10.4 1.6 6z" fill="currentColor" />
    </svg>
  );
}
