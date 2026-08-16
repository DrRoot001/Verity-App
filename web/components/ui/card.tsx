/** Surfaces (Verity Design Language v2). */

import type { ReactNode } from "react";

export function Card({
  children,
  as: Tag = "div",
  interactive = false,
}: {
  children: ReactNode;
  as?: "div" | "section" | "article";
  interactive?: boolean;
}) {
  const classes = [
    "rounded-[var(--radius-card)] border border-[var(--color-border-subtle)]",
    "bg-[var(--color-surface)] shadow-[var(--shadow-1)] overflow-hidden",
    interactive
      ? "transition-[box-shadow,border-color] duration-[var(--duration-panel)] ease-[var(--ease-standard)] hover:shadow-[var(--shadow-2)] hover:border-[var(--color-border-strong)]"
      : "",
  ].join(" ");

  return <Tag className={classes}>{children}</Tag>;
}

export function CardHeader({
  title,
  meta,
  action,
}: {
  title: string;
  meta?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-[var(--color-border-subtle)] px-5 py-4">
      <div className="min-w-0 space-y-1">
        <h2 className="text-md font-medium">{title}</h2>
        {meta ? (
          <p className="text-sm leading-relaxed text-[var(--color-text-secondary)]">{meta}</p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export function CardBody({ children, tight = false }: { children: ReactNode; tight?: boolean }) {
  return <div className={tight ? "px-5 py-3" : "px-5 py-4"}>{children}</div>;
}

/** A quiet strip below a body — counts, formulae, provenance. */
export function CardFooter({ children }: { children: ReactNode }) {
  return (
    <div className="border-t border-[var(--color-border-subtle)] bg-[var(--color-raised)] px-5 py-2.5 text-xs text-[var(--color-text-secondary)]">
      {children}
    </div>
  );
}

/** Page heading block, so every screen shares one vertical rhythm. */
export function PageHeader({
  title,
  description,
  action,
  children,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0 space-y-2">
        <h1 className="text-2xl">{title}</h1>
        {description ? (
          <p className="max-w-prose text-[var(--color-text-secondary)]">{description}</p>
        ) : null}
        {children}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </header>
  );
}
