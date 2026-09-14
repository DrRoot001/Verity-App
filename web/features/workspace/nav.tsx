"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
const TABS = [
  ["", "Overview"],
  ["preparation", "Prep"],
  ["job", "Job"],
  ["mocks", "Mocks"],
  ["live", "Live"],
  ["sessions", "History"],
  ["settings", "Settings"],
] as const;
export function WorkspaceNav({ id }: { id: string }) {
  const path = usePathname();
  return (
    <nav aria-label="Workspace" className="-mx-1 overflow-x-auto">
      <ul className="flex min-w-max gap-1 border-b border-[var(--color-border-subtle)] px-1">
        {TABS.map(([slug, label]) => {
          const href = slug ? `/workspaces/${id}/${slug}` : `/workspaces/${id}`;
          const active = path === href;
          return (
            <li key={slug}>
              <Link
                href={href}
                className={`block border-b-2 px-3 py-2 text-sm ${active ? "border-[var(--color-accent)] font-medium text-[var(--color-accent)]" : "border-transparent text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]"}`}
              >
                {label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
