"use client";

/**
 * Dashboard (PRD §10.4). Answers exactly four questions, in order:
 * what am I preparing for, what should I do next, how prepared am I,
 * what happened recently. Deliberately not an analytics surface.
 */

import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, PageHeader } from "@/components/ui/card";
import { Empty, ErrorState, Loading } from "@/components/ui/states";
import { reviewQueue } from "@/features/graph/api";
import { listWorkspaces } from "@/features/workspace/api";
import { ApiError } from "@/lib/api/client";
import type { ReviewQueue, WorkspaceSummary } from "@/lib/api/types";

export default function DashboardPage() {
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[] | null>(null);
  const [queue, setQueue] = useState<ReviewQueue | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    try {
      const [w, q] = await Promise.all([listWorkspaces(), reviewQueue()]);
      setWorkspaces(w);
      setQueue(q);
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
      else throw caught;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <ErrorState
        title="We couldn't load your dashboard"
        description={error.message}
        requestId={error.requestId}
        action={{ label: "Try again", onClick: () => void load() }}
      />
    );
  }

  if (!workspaces || !queue) return <Loading rows={3} label="Loading your dashboard" />;

  const next = workspaces
    .filter((w) => w.interview_at && !w.archived)
    .sort((a, b) => (a.interview_at ?? "").localeCompare(b.interview_at ?? ""))[0];

  return (
    <div className="space-y-8">
      <PageHeader
        title="Dashboard"
        description="What you're preparing for, and what to do next."
      />

      {queue.pending_count > 0 ? (
        <Card>
          <CardHeader
            title="Confirm your profile"
            meta={`${queue.pending_count} extracted item${queue.pending_count === 1 ? "" : "s"} awaiting review`}
            action={<Button size="sm" href="/review">Review now</Button>}
          />
          <CardBody>
            <p className="text-sm text-[var(--color-text-secondary)]">
              Verity only cites facts you have confirmed, so these are not in use yet.
            </p>
          </CardBody>
        </Card>
      ) : null}

      <section aria-labelledby="next-heading" className="space-y-3">
        <h2 id="next-heading" className="text-lg font-medium">
          What am I preparing for?
        </h2>
        {next ? (
          <Card>
            <CardHeader
              title={`${next.role_title} · ${next.company_name}`}
              meta={`${next.stage.replace(/_/g, " ")} · round ${next.round_index}`}
              action={<Button size="sm" href={`/workspaces/${next.id}`}>Open workspace</Button>}
            />
          </Card>
        ) : (
          <Empty
            title="No scheduled interview"
            description="Create a workspace for the role you're targeting and add its job description."
            action={{ label: "Create workspace", href: "/workspaces" }}
          />
        )}
      </section>

      <section aria-labelledby="workspaces-heading" className="space-y-3">
        <h2 id="workspaces-heading" className="text-lg font-medium">
          Your workspaces
        </h2>
        {workspaces.length === 0 ? (
          <Empty
            title="No workspaces yet"
            description="A workspace holds everything about one opportunity — the role, the job description, and your matched experience."
            action={{ label: "Create your first workspace", href: "/workspaces" }}
          />
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2">
            {workspaces.map((w) => (
              <li key={w.id}>
                <Card as="article" interactive>
                  <CardBody>
                    <div className="space-y-2">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="font-medium">{w.role_title}</p>
                          <p className="text-sm text-[var(--color-text-secondary)]">{w.company_name}</p>
                        </div>
                        <Badge tone={w.integrity_mode === "proctored" ? "warning" : "neutral"}>
                          {w.integrity_mode}
                        </Badge>
                      </div>
                      <Button size="sm" variant="secondary" href={`/workspaces/${w.id}`}>
                        Open
                      </Button>
                    </div>
                  </CardBody>
                </Card>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
