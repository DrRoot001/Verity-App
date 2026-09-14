"use client";

/**
 * Session readiness gate (PRD §8.2, §12).
 *
 * Both interview modes read one ContextBundle. If that bundle is thin — no job
 * description, no approved experience, no stories — the model has nothing
 * candidate-specific to ground in and falls back to generic frameworks. The
 * honest fix is to say so *before* the session, not to let someone sit through
 * twenty minutes of interview and conclude the product is useless.
 *
 * The blocking bar is deliberately low: approved experience is required because
 * without it there is literally no candidate. A missing job description or
 * story bank degrades quality rather than preventing the session, so those warn
 * instead of blocking.
 */

import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import type { ContextBundle } from "@/lib/api/types";

export interface ReadinessCheck {
  label: string;
  met: boolean;
  blocking: boolean;
  detail: string;
  href: string;
  action: string;
}

export function readinessChecks(bundle: ContextBundle, workspaceId: string): ReadinessCheck[] {
  const approved = bundle.candidate.approved_counts ?? {};
  const experiences = bundle.candidate.experiences.length;
  const stories = bundle.candidate.story_index?.length ?? 0;
  const pending = Object.values(bundle.candidate.pending_counts ?? {}).reduce(
    (total, count) => total + Number(count ?? 0),
    0,
  );

  return [
    {
      label: "Approved work history",
      met: experiences > 0,
      blocking: true,
      detail:
        experiences > 0
          ? `${experiences} experience${experiences === 1 ? "" : "s"} approved`
          : pending > 0
            ? `${pending} extracted item${pending === 1 ? "" : "s"} are waiting for your approval`
            : "Upload a resume so there is something to draw on",
      href: pending > 0 ? "/review" : "/profile",
      action: pending > 0 ? "Review them" : "Add your resume",
    },
    {
      label: "Job description",
      met: bundle.opportunity.jd_present,
      blocking: false,
      detail: bundle.opportunity.jd_present
        ? `${bundle.opportunity.must_have.length} requirements parsed`
        : "Without it, questions and coverage are inferred from the role title alone",
      href: `/workspaces/${workspaceId}/job`,
      action: "Add the job description",
    },
    {
      label: "Story bank",
      met: stories > 0,
      blocking: false,
      detail:
        stories > 0
          ? `${stories} stor${stories === 1 ? "y" : "ies"} ready to cite`
          : "Guidance can cite facts, but has no ready-made narratives to point at",
      href: "/stories",
      action: "Build stories",
    },
    {
      label: "Approved skills",
      met: Number(approved.skill ?? 0) > 0,
      blocking: false,
      detail:
        Number(approved.skill ?? 0) > 0
          ? `${approved.skill} skills approved`
          : "Technical questions will match on job-description terms only",
      href: "/review",
      action: "Approve skills",
    },
  ];
}

export function blocked(checks: ReadinessCheck[]): boolean {
  return checks.some((check) => check.blocking && !check.met);
}

export function ReadinessList({ checks }: { checks: ReadinessCheck[] }) {
  return (
    <ul className="space-y-2.5">
      {checks.map((check) => (
        <li key={check.label} className="flex flex-wrap items-start gap-x-3 gap-y-1">
          <span className="pt-0.5">
            <Badge tone={check.met ? "positive" : check.blocking ? "critical" : "warning"}>
              {check.met ? "Ready" : check.blocking ? "Required" : "Recommended"}
            </Badge>
          </span>
          <span className="min-w-0 flex-1">
            <span className="text-sm font-medium">{check.label}</span>
            <span className="block text-sm text-[var(--color-text-secondary)]">
              {check.detail}
            </span>
          </span>
          {check.met ? null : (
            <Link
              href={check.href}
              className="text-sm text-[var(--color-accent)] underline-offset-2 hover:underline"
            >
              {check.action}
            </Link>
          )}
        </li>
      ))}
    </ul>
  );
}
