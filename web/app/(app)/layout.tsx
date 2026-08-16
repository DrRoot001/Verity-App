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
      <div role="status" aria-live="polite" className="p-8 text-[var(--color-text-secondary)]">
        Checking your session…
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-[var(--color-border-subtle)] bg-[var(--color-surface)]">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-6 px-6 py-3">
          <div className="flex items-center gap-6">
            <Link href="/dashboard" className="font-semibold tracking-tight text-[var(--color-accent)]">
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
                        className={`rounded-[var(--radius-control)] px-3 py-1.5 text-sm ${
                          active
                            ? "bg-[var(--color-accent-quiet)] font-medium text-[var(--color-accent)]"
                            : "text-[var(--color-text-secondary)] hover:bg-[var(--color-raised)]"
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
            <span className="text-[var(--color-text-secondary)]">{email}</span>
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
      <main id="main" className="mx-auto max-w-6xl px-6 py-8">
        {children}
      </main>
    </div>
  );
}
