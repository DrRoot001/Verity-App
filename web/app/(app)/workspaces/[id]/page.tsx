"use client";

/**
 * Workspace overview (PRD §11.3).
 *
 * This screen is where the architecture becomes visible to the user: the match
 * is rendered requirement by requirement with the evidence that backs it, so
 * "strong" is always traceable to a specific approved record rather than being
 * an unexplained number (FR-JD-005).
 */

import { use, useCallback, useEffect, useState } from "react";

import { Badge, EvidenceChip } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Empty, ErrorState, Loading, Partial } from "@/components/ui/states";
import { getContext } from "@/features/workspace/api";
import { ApiError } from "@/lib/api/client";
import type { ContextBundle, MatchStatus, RequirementMatch } from "@/lib/api/types";

const STATUS_TONE: Record<MatchStatus, "positive" | "warning" | "critical"> = {
  strong: "positive",
  partial: "warning",
  gap: "critical",
};

export default function WorkspacePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [bundle, setBundle] = useState<ContextBundle | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    try {
      setBundle(await getContext(id));
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
      else throw caught;
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <ErrorState
        title={error.status === 404 ? "Workspace not found" : "We couldn't load this workspace"}
        description={error.message}
        requestId={error.requestId}
        action={{ label: "Back to workspaces", href: "/workspaces" }}
      />
    );
  }

  if (!bundle) return <Loading rows={4} label="Loading workspace context" />;

  const { opportunity, candidate, match } = bundle;

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">{opportunity.role_title}</h1>
        <p className="text-[var(--color-text-secondary)]">{opportunity.company_name}</p>
        <div className="flex flex-wrap gap-2">
          <Badge tone="neutral">{opportunity.stage.replace(/_/g, " ")}</Badge>
          {opportunity.seniority ? <Badge tone="info">{opportunity.seniority}</Badge> : null}
          <Badge tone={opportunity.jd_present ? "positive" : "warning"}>
            {opportunity.jd_present ? "job description attached" : "no job description"}
          </Badge>
          <Badge tone="neutral">{`context v${bundle.version}`}</Badge>
        </div>
      </header>

      {bundle.warnings.includes("pending_review_items") ? (
        <Partial missing="Some extracted facts are still awaiting your review" />
      ) : null}

      <section aria-labelledby="match-heading" className="space-y-3">
        <h2 id="match-heading" className="text-lg font-medium">
          How you match
        </h2>

        {match.computed_without_jd ? (
          <Empty
            title="No match computed"
            description="Add the job description to see a requirement-by-requirement match. Verity won't guess a score without one."
            action={{ label: "Back to workspaces", href: "/workspaces" }}
          />
        ) : (
          <Card>
            <CardHeader
              title={`${Math.round(match.overall_score)}% match`}
              meta={formulaSummary(match.formula)}
            />
            <CardBody>
              <ul className="divide-y divide-[var(--color-border-subtle)]">
                {match.requirements.map((requirement, index) => (
                  <RequirementRow key={index} requirement={requirement} />
                ))}
              </ul>
            </CardBody>
          </Card>
        )}
      </section>

      <section aria-labelledby="evidence-heading" className="space-y-3">
        <h2 id="evidence-heading" className="text-lg font-medium">
          Your confirmed experience
        </h2>
        {candidate.experiences.length === 0 ? (
          <Empty
            title="No approved experience yet"
            description="Confirm the facts extracted from your resume so Verity can cite them."
            action={{ label: "Review profile", href: "/review" }}
          />
        ) : (
          <Card>
            <CardBody>
              <ul className="space-y-4">
                {candidate.experiences.map((experience) => (
                  <li key={experience.id} className="space-y-1">
                    <p className="font-medium">
                      {experience.title} · {experience.company}
                    </p>
                    <p className="text-xs text-[var(--color-text-muted)]">
                      {experience.start ?? "—"} to {experience.is_current ? "present" : (experience.end ?? "—")}
                    </p>
                    {experience.achievements.length > 0 ? (
                      <ul className="mt-1 space-y-1">
                        {experience.achievements.map((achievement) => (
                          <li key={achievement.id} className="text-sm text-[var(--color-text-secondary)]">
                            {achievement.statement}
                            {achievement.has_metric ? (
                              <span className="ml-2 text-xs text-[var(--color-positive)]">quantified</span>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </li>
                ))}
              </ul>
            </CardBody>
          </Card>
        )}
      </section>

      {candidate.story_index.length > 0 ? (
        <section aria-labelledby="stories-heading" className="space-y-3">
          <h2 id="stories-heading" className="text-lg font-medium">
            Approved stories
          </h2>
          <Card>
            <CardBody>
              <ul className="space-y-2">
                {candidate.story_index.map((story) => (
                  <li key={story.id} className="flex items-center justify-between gap-3">
                    <span>{story.title}</span>
                    {story.speak_time_seconds ? (
                      <span className="text-xs text-[var(--color-text-muted)]">
                        ~{story.speak_time_seconds}s to tell
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </CardBody>
          </Card>
        </section>
      ) : null}
    </div>
  );
}

function RequirementRow({ requirement }: { requirement: RequirementMatch }) {
  return (
    <li className="space-y-1.5 py-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm">{requirement.requirement}</p>
        <Badge tone={STATUS_TONE[requirement.status]}>{requirement.status}</Badge>
      </div>
      <p className="text-xs text-[var(--color-text-muted)]">{requirement.rationale}</p>
      {requirement.evidence.length > 0 ? (
        <div className="flex flex-wrap gap-1.5 pt-1">
          {requirement.evidence.map((evidence) => (
            <EvidenceChip key={evidence.id} label={evidence.label} source={evidence.type} />
          ))}
        </div>
      ) : null}
    </li>
  );
}

function formulaSummary(formula: Record<string, number>): string {
  const parts = Object.entries(formula).map(
    ([key, weight]) => `${key.replace(/_/g, " ")} ${Math.round(weight * 100)}%`,
  );
  return `Weighted by ${parts.join(", ")}`;
}
