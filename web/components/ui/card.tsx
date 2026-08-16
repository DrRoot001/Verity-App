/** Surface primitives (PRD §23.5). */

import type { ReactNode } from "react";

export function Card({ children, as: Tag = "div" }: { children: ReactNode; as?: "div" | "section" | "article" }) {
  return (
    <Tag className="rounded-[var(--radius-card)] border border-[var(--color-border-subtle)] bg-[var(--color-surface)] shadow-[var(--shadow-1)]">
      {children}
    </Tag>
  );
}

export function CardHeader({ title, meta, action }: { title: string; meta?: string; action?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-[var(--color-border-subtle)] px-5 py-4">
      <div className="space-y-0.5">
        <h2 className="text-md font-medium text-[var(--color-text-primary)]">{title}</h2>
        {meta ? <p className="text-sm text-[var(--color-text-secondary)]">{meta}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function CardBody({ children }: { children: ReactNode }) {
  return <div className="px-5 py-4">{children}</div>;
}
