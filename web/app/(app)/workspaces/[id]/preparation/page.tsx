"use client";

/**
 * Preparation plan (PRD §10.8).
 *
 * Two things this screen must do that a generic task list would not: show the
 * readiness score's drivers rather than the number alone (FR-PREP-004), and let
 * every task explain its own ranking on demand (FR-PREP-002). An opaque
 * ordering trains people to ignore it.
 */

import { use, useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Empty, ErrorState, Loading } from "@/components/ui/states";
import {
  generatePlan,
  getPlan,
  setTaskStatus,
  type PrepPlan,
  type PrepTask,
} from "@/features/workspace/preparation";
import { ApiError } from "@/lib/api/client";

const PRIORITY_TONE = {
  critical: "critical",
  high: "warning",
  medium: "info",
  optional: "neutral",
} as const;

export default function PreparationPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [plan, setPlan] = useState<PrepPlan | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPlan(await getPlan(id));
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
      else throw caught;
    } finally {
      setLoaded(true);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function regenerate() {
    setBusy(true);
    setError(null);
    try {
      setPlan(await generatePlan(id));
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
      else throw caught;
    } finally {
      setBusy(false);
    }
  }

  async function toggle(task: PrepTask) {
    const next = task.status === "done" ? "open" : "done";
    await setTaskStatus(task.id, next);
    await load();
  }

  if (error && !plan) {
    return (
      <ErrorState
        title="We couldn't load your plan"
        description={error.message}
        requestId={error.requestId}
        recovery={error.recoveryAction}
        action={{ label: "Try again", onClick: () => void load() }}
      />
    );
  }

  if (!loaded) return <Loading rows={4} label="Loading your preparation plan" />;

  if (!plan) {
    return (
      <Empty
        title="No preparation plan yet"
        description="Verity turns the gaps between your confirmed experience and this role's requirements into ranked, launchable work."
        action={{ label: busy ? "Generating…" : "Generate plan", onClick: () => void regenerate() }}
      />
    );
  }

  const open = plan.tasks.filter((t) => t.status !== "done" && t.status !== "dismissed");
  const done = plan.tasks.filter((t) => t.status === "done");

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">Preparation</h1>
          <p className="text-sm text-[var(--color-text-secondary)]">
            Round {plan.round_index} · plan v{plan.version}
          </p>
        </div>
        <Button variant="secondary" size="sm" loading={busy} onClick={() => void regenerate()}>
          Regenerate
        </Button>
      </header>

      <Card>
        <CardHeader
          title={`${Math.round(plan.readiness.score)}% ready`}
          meta="How much of the identified preparation is done — not a prediction of the outcome."
        />
        <CardBody>
          <ul className="space-y-2">
            {plan.readiness.drivers.map((driver) => (
              <li key={driver.factor} className="flex items-start justify-between gap-4 text-sm">
                <div>
                  <p className="font-medium">{driver.factor.replace(/_/g, " ")}</p>
                  <p className="text-xs text-[var(--color-text-muted)]">{driver.detail}</p>
                </div>
                <span className="shrink-0 text-xs text-[var(--color-text-secondary)]">
                  {Math.round(driver.value * 100)}% × {Math.round(driver.weight * 100)}%
                </span>
              </li>
            ))}
          </ul>
        </CardBody>
      </Card>

      <section aria-labelledby="tasks-heading" className="space-y-3">
        <h2 id="tasks-heading" className="text-lg font-medium">
          What to do next ({open.length})
        </h2>
        {open.length === 0 ? (
          <Empty title="Everything is done" description="Regenerate the plan if your context changed." />
        ) : (
          <Card>
            <CardBody>
              <ul className="divide-y divide-[var(--color-border-subtle)]">
                {open.map((task) => (
                  <li key={task.id} className="space-y-2 py-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="space-y-1">
                        <p className="font-medium">{task.title}</p>
                        {task.detail ? (
                          <p className="text-sm text-[var(--color-text-secondary)]">{task.detail}</p>
                        ) : null}
                        <p className="text-xs text-[var(--color-text-muted)]">
                          {task.estimated_minutes} min
                          {task.scheduled_for ? ` · scheduled ${task.scheduled_for}` : ""}
                        </p>
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        <Badge tone={PRIORITY_TONE[task.priority]}>{task.priority}</Badge>
                        <Button size="sm" variant="secondary" onClick={() => void toggle(task)}>
                          Done
                        </Button>
                      </div>
                    </div>

                    <button
                      type="button"
                      aria-expanded={expanded === task.id}
                      onClick={() => setExpanded(expanded === task.id ? null : task.id)}
                      className="text-xs text-[var(--color-accent)] underline underline-offset-2"
                    >
                      {expanded === task.id ? "Hide" : "Why this ranking?"}
                    </button>

                    {expanded === task.id ? (
                      <dl className="grid gap-1 rounded-[var(--radius-control)] bg-[var(--color-raised)] p-3 text-xs sm:grid-cols-2">
                        {Object.entries(task.score_breakdown.factors ?? {}).map(([factor, value]) => (
                          <div key={factor} className="flex justify-between gap-2">
                            <dt className="text-[var(--color-text-secondary)]">
                              {factor.replace(/_/g, " ")}
                            </dt>
                            <dd>
                              {value.toFixed(2)} ×{" "}
                              {(task.score_breakdown.weights?.[factor] ?? 0).toFixed(2)} ={" "}
                              {(task.score_breakdown.contributions?.[factor] ?? 0).toFixed(3)}
                            </dd>
                          </div>
                        ))}
                        <div className="flex justify-between gap-2 font-medium sm:col-span-2">
                          <dt>Total</dt>
                          <dd>{(task.score_breakdown.total ?? 0).toFixed(3)}</dd>
                        </div>
                      </dl>
                    ) : null}
                  </li>
                ))}
              </ul>
            </CardBody>
          </Card>
        )}
      </section>

      {done.length > 0 ? (
        <section aria-labelledby="done-heading" className="space-y-3">
          <h2 id="done-heading" className="text-lg font-medium">
            Completed ({done.length})
          </h2>
          <Card>
            <CardBody>
              <ul className="space-y-1.5">
                {done.map((task) => (
                  <li key={task.id} className="flex items-center justify-between gap-3 text-sm">
                    <span className="text-[var(--color-text-secondary)] line-through">
                      {task.title}
                    </span>
                    <Button size="sm" variant="ghost" onClick={() => void toggle(task)}>
                      Reopen
                    </Button>
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
