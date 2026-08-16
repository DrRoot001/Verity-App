"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { bootstrapSession, clearSession, currentUser, isSignedIn } from "@/features/auth/session";

const NAV = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/review", label: "Profile review" },
  { href: "/workspaces", label: "Workspaces" },
] as const;

export default function AppLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [ready, setReady] = useState(false);
  const [email, setEmail] = useState<string | null>(null);

  useEffect(() => {
    bootstrapSession();
    if (!isSignedIn()) {
      router.replace("/login");
      return;
    }
    setEmail(currentUser()?.email ?? null);
    setReady(true);
  }, [router]);

  if (!ready) {
    return (
      <div role="status" aria-live="polite" className="p-10 text-[var(--color-text-secondary)]">
        Checking your session…
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-[var(--color-border-subtle)] bg-[var(--color-surface)]/85 backdrop-blur-md">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-6 px-6 py-3">
          <div className="flex items-center gap-6">
            <Link
              href="/dashboard"
              className="flex items-center gap-2 text-md font-semibold tracking-tight"
            >
              <span
                aria-hidden="true"
                className="grid size-6 place-items-center rounded-[6px] bg-[var(--color-accent)] text-[11px] font-bold text-[var(--color-accent-contrast)]"
              >
                V
              </span>
              Verity
            </Link>
            <nav aria-label="Main">
              <ul className="flex gap-1">
                {NAV.map((item) => {
                  const active = pathname.startsWith(item.href);
                  return (
                    <li key={item.href}>
                      <Link
                        href={item.href}
                        aria-current={active ? "page" : undefined}
                        className={`rounded-[var(--radius-control)] px-3 py-1.5 text-sm transition-colors duration-[var(--duration-micro)] ${
                          active
                            ? "bg-[var(--color-accent-quiet)] font-medium text-[var(--color-accent)]"
                            : "text-[var(--color-text-secondary)] hover:bg-[var(--color-raised)] hover:text-[var(--color-text-primary)]"
                        }`}
                      >
                        {item.label}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </nav>
          </div>
          <div className="flex items-center gap-3 text-sm">
            <span className="hidden text-[var(--color-text-secondary)] sm:inline">{email}</span>
            <button
              type="button"
              onClick={() => {
                clearSession();
                router.replace("/login");
              }}
              className="rounded-[var(--radius-control)] px-2 py-1 text-[var(--color-text-secondary)] hover:bg-[var(--color-raised)]"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-6xl px-6 py-10">
        {children}
      </main>
    </div>
  );
}
