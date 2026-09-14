/* eslint-disable max-lines */
/**
 * The eleven UX states as primitives (PRD §10.1).
 *
 * These exist as components rather than as a convention because the PRD makes
 * them a shipping requirement: a view is not done until every applicable state
 * is handled. Making them importable means the cost of doing it right is lower
 * than the cost of improvising a spinner.
 *
 * Two rules are enforced here rather than left to the caller:
 *   - No indefinite spinner. `Processing` names its current stage.
 *   - No fake progress. `Processing` accepts a measured value or none at all.
 */

import type { ReactNode } from "react";

import { Button } from "./button";

interface StateProps {
  title: string;
  description?: string;
  action?: { label: string; onClick?: () => void; href?: string };
  icon?: ReactNode;
}

function StateShell({
  title,
  description,
  action,
  icon,
  tone = "neutral",
}: StateProps & { tone?: "neutral" | "critical" | "warning" | "info" }) {
  const toneStyles = {
    neutral: "border-[var(--color-border-subtle)] bg-[var(--color-surface)]",
    critical: "border-[var(--color-critical)] bg-[var(--color-critical-quiet)]",
    warning: "border-[var(--color-warning)] bg-[var(--color-warning-quiet)]",
    info: "border-[var(--color-info)] bg-[var(--color-info-quiet)]",
  }[tone];

  return (
    <div
      className={`flex flex-col items-start gap-3 rounded-[var(--radius-card)] border p-6 ${toneStyles}`}
    >
      {icon ? (
        <span aria-hidden="true" className="text-lg">
          {icon}
        </span>
      ) : null}
      <div className="space-y-1">
        <p className="text-md font-medium text-[var(--color-text-primary)]">{title}</p>
        {description ? (
          <p className="max-w-prose text-[var(--color-text-secondary)]">{description}</p>
        ) : null}
      </div>
      {action ? (
        <Button variant="secondary" size="sm" onClick={action.onClick} href={action.href}>
          {action.label}
        </Button>
      ) : null}
    </div>
  );
}

/** Skeleton matching the final layout. Never a bare spinner (PRD §10.1). */
export function Loading({ rows = 3, label }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true" className="space-y-3">
      <span className="sr-only">{label ?? "Loading"}</span>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="space-y-2 rounded-[var(--radius-card)] p-4">
          <div className="skeleton h-3 w-1/3 rounded" />
          <div className="skeleton h-3 w-2/3 rounded" />
        </div>
      ))}
    </div>
  );
}

/** Cause, one action, and what happens next — never an illustration alone. */
export function Empty(props: StateProps) {
  return <StateShell {...props} tone="neutral" />;
}

/**
 * Errors always carry the backend's recovery action and a copyable request id,
 * so support can find the trace (FR-ERR-001).
 *
 * When an ApiError is passed, its own recovery_action wins over any default the
 * caller supplied — the server knows whether the fix is "retry", "verify your
 * email" or "upgrade", and a page-level guess would override the truth.
 */
export function ErrorState({
  title,
  description,
  action,
  requestId,
  recovery,
}: StateProps & {
  requestId?: string | null;
  recovery?: { type: string; target?: string | null; label?: string | null };
}) {
  const resolved = recovery ? (recoveryToAction(recovery) ?? action) : action;
  return (
    <div className="space-y-2">
      <StateShell
        title={title}
        description={description}
        action={resolved}
        tone="critical"
        icon="⚠"
      />
      {requestId ? (
        <p className="text-xs text-[var(--color-text-muted)]">
          Reference: <code className="font-[var(--font-mono)]">{requestId}</code>
        </p>
      ) : null}
    </div>
  );
}

function recoveryToAction(recovery: {
  type: string;
  target?: string | null;
  label?: string | null;
}): StateProps["action"] {
  const labels: Record<string, string> = {
    verify_email: "Resend verification",
    reauthenticate: "Sign in again",
    upgrade: "See plans",
    edit_input: "Review your input",
    contact_support: "Contact support",
    grant_permission: "Open settings",
    reconnect: "Reconnect",
  };
  if (recovery.type === "none") return undefined;
  const label = recovery.label ?? labels[recovery.type];
  if (!label) return undefined;
  return recovery.target ? { label, href: recovery.target } : { label };
}

/** Some sub-resources failed; the rest still renders (PRD §10.1 `partial`). */
export function Partial({ missing, onRetry }: { missing: string; onRetry?: () => void }) {
  return (
    <div
      role="status"
      className="flex items-center justify-between gap-4 rounded-[var(--radius-control)] border border-[var(--color-warning)] bg-[var(--color-warning-quiet)] px-4 py-2 text-sm"
    >
      <span className="text-[var(--color-text-primary)]">
        <span aria-hidden="true">⚠ </span>
        {missing} couldn&apos;t be loaded.
      </span>
      {onRetry ? (
        <Button variant="ghost" size="sm" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}

export function PermissionDenied({
  what,
  how,
  target,
}: {
  what: string;
  how: string;
  target?: string;
}) {
  return (
    <StateShell
      title={`${what} permission is needed`}
      description={how}
      tone="info"
      icon="🔒"
      {...(target ? { action: { label: "Open settings", href: target } } : {})}
    />
  );
}

export function Offline({ lastUpdated }: { lastUpdated?: string }) {
  return (
    <StateShell
      title="You're offline"
      description={
        lastUpdated
          ? `Showing data from ${lastUpdated}. Changes will be saved when you reconnect.`
          : "Showing cached data. Changes will be saved when you reconnect."
      }
      tone="warning"
      icon="⚡"
    />
  );
}

export function RateLimited({ resetsIn, limitName }: { resetsIn: number; limitName?: string }) {
  return (
    <StateShell
      title={`Too many ${limitName ?? "requests"}`}
      description={`Try again in ${resetsIn} second${resetsIn === 1 ? "" : "s"}.`}
      tone="warning"
      icon="⏱"
    />
  );
}

export function SubscriptionLocked({
  entitlement,
  currentPlan,
  unlocks,
}: {
  entitlement: string;
  currentPlan: string;
  unlocks: string;
}) {
  return (
    <StateShell
      title={`${entitlement} isn't included in ${currentPlan}`}
      description={unlocks}
      tone="info"
      icon="✦"
      action={{ label: "See plans", href: "/settings/billing" }}
    />
  );
}

/** Data older than its freshness SLA must say so (PRD FR-DOC-021). */
export function Stale({
  retrievedAt,
  onRefresh,
}: {
  retrievedAt: string;
  onRefresh?: () => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4 rounded-[var(--radius-control)] border border-[var(--color-border-subtle)] bg-[var(--color-raised)] px-4 py-2 text-sm">
      <span className="text-[var(--color-text-secondary)]">
        Last updated {retrievedAt}. This may be out of date.
      </span>
      {onRefresh ? (
        <Button variant="ghost" size="sm" onClick={onRefresh}>
          Refresh
        </Button>
      ) : null}
    </div>
  );
}

/**
 * Named stages, and a measured value or nothing. A percentage we cannot
 * actually measure is a lie the user will calibrate against (PRD §10.1).
 */
export function Processing({
  stages,
  currentStage,
  measuredPercent,
}: {
  stages: string[];
  currentStage: string;
  measuredPercent?: number;
}) {
  const currentIndex = stages.indexOf(currentStage);
  return (
    <div role="status" aria-live="polite" className="space-y-3">
      <ol className="space-y-1.5">
        {stages.map((stage, index) => {
          const done = index < currentIndex;
          const active = index === currentIndex;
          return (
            <li key={stage} className="flex items-center gap-2 text-sm">
              <span aria-hidden="true" className="w-4 text-center">
                {done ? "✓" : active ? "•" : "·"}
              </span>
              <span
                className={
                  active
                    ? "font-medium text-[var(--color-text-primary)]"
                    : "text-[var(--color-text-secondary)]"
                }
              >
                {stage}
              </span>
              {active ? <span className="sr-only">(in progress)</span> : null}
            </li>
          );
        })}
      </ol>
      {measuredPercent !== undefined ? (
        <div
          role="progressbar"
          aria-valuenow={Math.round(measuredPercent)}
          aria-valuemin={0}
          aria-valuemax={100}
          className="h-1.5 w-full overflow-hidden rounded-full bg-[var(--color-raised)]"
        >
          <div
            className="h-full bg-[var(--color-accent)] transition-[width] duration-200"
            style={{ width: `${measuredPercent}%` }}
          />
        </div>
      ) : null}
    </div>
  );
}
