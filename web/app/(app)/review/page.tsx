"use client";

/**
 * Profile review (PRD §10.3 step 7, FR-ONB-002).
 *
 * The review gate made visible: nothing extracted from a resume is usable
 * until a person confirms it. Every card shows where the fact came from and
 * how confident the parser was, because the user is being asked to vouch for
 * it — not to rubber-stamp a black box.
 */

import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Empty, ErrorState, Loading, Processing } from "@/components/ui/states";
import { bulkApprove, pasteResume, rejectNode, reviewQueue } from "@/features/graph/api";
import { ApiError } from "@/lib/api/client";
import type { GraphNode, ReviewQueue } from "@/lib/api/types";

const INGEST_STAGES = ["Reading document", "Detecting sections", "Extracting entities"];

export default function ReviewPage() {
  const [queue, setQueue] = useState<ReviewQueue | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pasting, setPasting] = useState(false);
  const [draft, setDraft] = useState("");

  const load = useCallback(async () => {
    try {
      setQueue(await reviewQueue());
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
      else throw caught;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function approveAll(entityType: string, nodes: GraphNode[]) {
    setBusy(entityType);
    try {
      await bulkApprove(
        entityType,
        nodes.map((n) => n.id),
      );
      await load();
    } finally {
      setBusy(null);
    }
  }

  async function reject(node: GraphNode) {
    setBusy(node.id);
    try {
      await rejectNode(node.entity_type, node.id);
      await load();
    } finally {
      setBusy(null);
    }
  }

  async function importResume() {
    setPasting(true);
    setError(null);
    try {
      await pasteResume(draft);
      setDraft("");
      await load();
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
      else throw caught;
    } finally {
      setPasting(false);
    }
  }

  if (error && !queue) {
    return (
      <ErrorState
        title="We couldn't load your profile"
        description={error.message}
        requestId={error.requestId}
        action={{ label: "Try again", onClick: () => void load() }}
      />
    );
  }

  if (!queue) return <Loading rows={3} label="Loading your review queue" />;

  const byType = groupByType(queue.pending);

  return (
    <div className="space-y-8">
      <header className="space-y-1">
        <h1 className="text-2xl">Review your profile</h1>
        <p className="max-w-prose text-[var(--color-text-secondary)]">
          Nothing here is used in an interview until you confirm it. Approving a fact is what
          lets Verity cite it later.
        </p>
        <div className="flex gap-2 pt-1">
          <Badge tone="positive">{`${queue.approved_count} approved`}</Badge>
          <Badge tone="warning">{`${queue.pending_count} awaiting review`}</Badge>
          {queue.rejected_count > 0 ? (
            <Badge tone="neutral">{`${queue.rejected_count} rejected`}</Badge>
          ) : null}
        </div>
      </header>

      {queue.pending.length === 0 ? (
        <Card>
          <CardHeader title="Nothing to review" meta="Import a resume to get started." />
          <CardBody>
            <div className="space-y-3">
              <label htmlFor="resume" className="block text-sm font-medium">
                Paste your resume
              </label>
              <textarea
                id="resume"
                rows={10}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="Paste the full text of your resume here."
                className="field font-[var(--font-mono)] text-sm"
              />
              {error ? (
                <p role="alert" className="text-sm text-[var(--color-critical)]">
                  {error.message}
                </p>
              ) : null}
              {pasting ? (
                <Processing stages={INGEST_STAGES} currentStage="Extracting entities" />
              ) : (
                <Button onClick={() => void importResume()} disabled={draft.trim().length < 100}>
                  Import resume
                </Button>
              )}
            </div>
          </CardBody>
        </Card>
      ) : (
        Object.entries(byType).map(([entityType, nodes]) => (
          <Card key={entityType}>
            <CardHeader
              title={`${label(entityType)} (${nodes.length})`}
              meta="Extracted from your resume — confirm what's accurate."
              action={
                <Button
                  size="sm"
                  loading={busy === entityType}
                  onClick={() => void approveAll(entityType, nodes)}
                >
                  Approve all
                </Button>
              }
            />
            <CardBody>
              <ul className="divide-y divide-[var(--color-border-subtle)]">
                {nodes.map((node) => (
                  <li key={node.id} className="flex items-start justify-between gap-4 py-3">
                    <div className="space-y-1">
                      <p className="font-medium">{node.label}</p>
                      <p className="text-xs text-[var(--color-text-muted)]">
                        From {node.provenance.source}
                        {node.provenance.confidence !== null
                          ? ` · parser confidence ${Math.round(node.provenance.confidence * 100)}%`
                          : ""}
                        {node.provenance.has_user_corrections ? " · edited by you" : ""}
                      </p>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={busy === node.id}
                        onClick={() => void approveAll(entityType, [node])}
                      >
                        Approve
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => void reject(node)}>
                        Reject
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            </CardBody>
          </Card>
        ))
      )}

      {queue.pending.length === 0 && queue.approved_count > 0 ? (
        <Empty
          title="Everything is reviewed"
          description="Your approved facts are now available to every workspace."
          action={{ label: "Go to dashboard", href: "/dashboard" }}
        />
      ) : null}
    </div>
  );
}

function groupByType(nodes: GraphNode[]): Record<string, GraphNode[]> {
  return nodes.reduce<Record<string, GraphNode[]>>((acc, node) => {
    (acc[node.entity_type] ??= []).push(node);
    return acc;
  }, {});
}

function label(entityType: string): string {
  const labels: Record<string, string> = {
    experience: "Experience",
    skill: "Skills",
    education: "Education",
    achievement: "Achievements",
    project: "Projects",
    certification: "Certifications",
  };
  return labels[entityType] ?? entityType;
}
