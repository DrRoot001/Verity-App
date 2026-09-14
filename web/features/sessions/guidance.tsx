"use client";

/** Guidance rendering for the live copilot (PRD §14.5). */

import { EvidenceChip } from "@/components/ui/badge";

export interface Answer {
  spoken_answer?: string;
  answer_direction?: string;
  key_points?: Array<{ text: string; claim_type: string; evidence_ids?: string[] }>;
  structure?: string;
  evidence?: Array<{ id?: string; label?: string; type?: string }>;
}

export function GuidancePanel({
  answer,
  latency,
  generating,
  unverified = [],
}: {
  answer: Answer | null;
  latency: Record<string, number>;
  generating: boolean;
  /** Names and figures in the spoken answer that no approved evidence backs. */
  unverified?: string[];
}) {
  if (generating && !answer) {
    return (
      <div className="mt-4 min-h-44 animate-pulse rounded-lg bg-[var(--color-raised)] p-4 text-sm text-[var(--color-text-secondary)]">
        Working out your answer…
      </div>
    );
  }

  if (!answer) {
    return (
      <div className="mt-4 min-h-44 rounded-lg bg-[var(--color-raised)] p-4 text-sm text-[var(--color-text-secondary)]">
        Turn the microphone on. When the interviewer asks something, guidance appears here on
        its own.
      </div>
    );
  }

  return (
    <div className="mt-3 space-y-5">
      {/* The words to say. Sized to be read aloud at a glance — the direction
          line is the same answer compressed, so it only shows without one. */}
      {answer.spoken_answer ? (
        <p className="whitespace-pre-wrap text-[1.15rem] leading-relaxed">
          {answer.spoken_answer}
        </p>
      ) : (
        <p className="text-[1.35rem] font-medium leading-snug text-[var(--color-accent)]">
          {answer.answer_direction}
        </p>
      )}
      {unverified.length ? (
        <p className="rounded-lg bg-[var(--color-raised)] p-2 text-xs text-[var(--color-warning,#f0b429)]">
          Not in your approved evidence: {unverified.join(", ")} — check before saying it.
        </p>
      ) : null}
      <ul className="space-y-2">
        {answer.key_points?.map((point, index) => (
          <li className="flex gap-2 text-[15px]" key={index}>
            <span className="text-[var(--color-accent)]">•</span>
            <span>
              {point.text}
              {/* A fact with no evidence was downgraded by the validator; say so
                  rather than let it read as something from the user's history. */}
              {point.claim_type === "general_advice" ? (
                <span className="ml-1.5 text-xs text-[var(--color-text-muted)]">general</span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
      {answer.structure ? (
        <p className="rounded-lg bg-[var(--color-raised)] p-3 text-sm">
          <strong>Structure:</strong> {answer.structure}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        {answer.evidence?.map((item, index) => (
          <EvidenceChip
            key={index}
            label={item.label ?? "Approved evidence"}
            source={item.type}
          />
        ))}
      </div>
      <p className="numeric text-xs text-[var(--color-text-muted)]">
        End-to-end {Math.round(latency.e2e ?? 0)} ms · generation{" "}
        {Math.round(latency.generation ?? 0)} ms
      </p>
    </div>
  );
}
