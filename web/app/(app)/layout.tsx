"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { Logo } from "@/components/brand/logo";
import { whoami, type StaffPrincipal } from "@/features/admin/api";
import {
  bootstrapSession,
  clearSession,
  currentUser,
  isSignedIn,
} from "@/features/auth/session";

const CANDIDATE_NAV = [
  ["/dashboard", "Overview", "⌂"],
  ["/workspaces", "Workspaces", "◇"],
  ["/sessions", "Interviews", "◉"],
  ["/profile", "Profile", "◎"],
  ["/stories", "Story bank", "✦"],
  ["/documents", "Documents", "▤"],
] as const;
const ADMIN_NAV = [
  ["/admin", "Operations", "⌂"],
  ["/admin/users", "Users", "◎"],
  ["/admin/sessions", "Sessions", "◉"],
  ["/admin/staff", "Staff & roles", "♙"],
  ["/admin/prompts", "Prompts", "✦"],
  ["/admin/flags", "Feature flags", "⚑"],
  ["/admin/audit", "Audit log", "▤"],
  ["/admin/settings", "AI settings", "⚙"],
] as const;

export default function AppLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [ready, setReady] = useState(false);
  const [staff, setStaff] = useState<StaffPrincipal | null>(null);
  const [menu, setMenu] = useState(false);
  const user = currentUser();

  useEffect(() => {
    bootstrapSession();
    if (!isSignedIn()) {
      router.replace("/login");
      return;
    }
    void whoami()
      .then((principal) => {
        setStaff(principal);
        if (pathname === "/dashboard") router.replace("/admin");
      })
      .catch(() => setStaff(null))
      .finally(() => setReady(true));
  }, [pathname, router]);

  if (!ready)
    return (
      <div className="grid min-h-screen place-items-center text-sm text-[var(--color-text-secondary)]">
        Preparing your workspace…
      </div>
    );
  const nav = staff ? ADMIN_NAV : CANDIDATE_NAV;
  const accountHref = staff ? "/admin/settings" : "/settings";

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[15.5rem_1fr]">
      {menu ? (
        <button
          aria-label="Close menu"
          className="fixed inset-0 z-30 bg-black/30 lg:hidden"
          onClick={() => setMenu(false)}
        />
      ) : null}
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-[17rem] flex-col border-r border-[var(--color-border-subtle)] bg-[var(--color-surface)] p-4 transition-transform lg:sticky lg:top-0 lg:h-screen lg:w-auto ${menu ? "translate-x-0" : "-translate-x-full lg:translate-x-0"}`}
      >
        <div className="flex h-11 items-center justify-between px-2">
          <Logo />
          <button
            className="rounded p-2 lg:hidden"
            onClick={() => setMenu(false)}
            aria-label="Close navigation"
          >
            ×
          </button>
        </div>
        <div className="mt-6 px-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-[var(--color-text-muted)]">
          {staff ? "Staff workspace" : "Candidate workspace"}
        </div>
        <nav className="mt-2 flex-1" aria-label="Primary navigation">
          <ul className="space-y-1">
            {nav.map(([href, label, icon]) => {
              const active = href === "/admin" ? pathname === href : pathname.startsWith(href);
              return (
                <li key={href}>
                  <Link
                    onClick={() => setMenu(false)}
                    href={href}
                    className={`flex h-10 items-center gap-3 rounded-[var(--radius-control)] px-3 text-sm font-medium transition-colors ${active ? "bg-[var(--color-accent-quiet)] text-[var(--color-accent)]" : "text-[var(--color-text-secondary)] hover:bg-[var(--color-raised)] hover:text-[var(--color-text-primary)]"}`}
                  >
                    <span aria-hidden="true" className="grid w-4 place-items-center text-base">
                      {icon}
                    </span>
                    {label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
        <div className="border-t border-[var(--color-border-subtle)] pt-3">
          <Link
            href={accountHref}
            className="flex items-center gap-3 rounded-[var(--radius-control)] p-2 hover:bg-[var(--color-raised)]"
          >
            <span className="grid size-8 place-items-center rounded-full bg-[var(--color-accent-quiet)] text-xs font-semibold text-[var(--color-accent)]">
              {(user?.full_name ?? user?.email ?? "V").slice(0, 1).toUpperCase()}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium">
                {user?.full_name ?? "Verity user"}
              </span>
              <span className="block truncate text-xs text-[var(--color-text-muted)]">
                {staff?.roles.join(", ") ?? user?.email}
              </span>
            </span>
          </Link>
          <button
            className="mt-1 w-full rounded px-3 py-2 text-left text-xs text-[var(--color-text-muted)] hover:bg-[var(--color-raised)]"
            onClick={() => {
              clearSession();
              router.replace("/login");
            }}
          >
            Sign out
          </button>
        </div>
      </aside>
      <div className="min-w-0">
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-[var(--color-border-subtle)] bg-[var(--color-canvas)]/90 px-4 backdrop-blur lg:hidden">
          <button
            onClick={() => setMenu(true)}
            className="grid size-9 place-items-center rounded border border-[var(--color-border-subtle)]"
            aria-label="Open navigation"
          >
            ☰
          </button>
          <Logo compact />
          <Link
            href={accountHref}
            className="grid size-9 place-items-center rounded-full bg-[var(--color-accent-quiet)] text-xs font-semibold text-[var(--color-accent)]"
          >
            {(user?.email ?? "V").charAt(0).toUpperCase()}
          </Link>
        </header>
        <main
          id="main"
          className="mx-auto w-full max-w-[88rem] px-4 py-6 sm:px-6 lg:px-10 lg:py-9"
        >
          {children}
        </main>
      </div>
    </div>
  );
}
