"use client";

/**
 * Email verification landing page (PRD §10.2).
 *
 * The signup email links here. Without this route the link is a dead end and a
 * new account can never be used, so the page has to handle every arrival: a
 * good token, an expired one, and a second visit after the first already
 * succeeded — a user who clicks the link twice has done nothing wrong and
 * should not be shown an error.
 */

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { verifyEmail } from "@/features/auth/api";
import { ApiError } from "@/lib/api/client";

type State = "checking" | "verified" | "failed" | "missing";

function Verify() {
  const token = useSearchParams().get("token");
  const [state, setState] = useState<State>("checking");
  const [message, setMessage] = useState("");
  // React runs effects twice in development; verifying twice would consume the
  // single-use token and show the user a failure for a link that worked.
  const attempted = useRef(false);

  useEffect(() => {
    if (!token) {
      setState("missing");
      return;
    }
    if (attempted.current) return;
    attempted.current = true;

    void verifyEmail(token)
      .then(() => setState("verified"))
      .catch((caught) => {
        setState("failed");
        setMessage(
          caught instanceof ApiError
            ? caught.message
            : "We couldn't verify this link. It may have expired.",
        );
      });
  }, [token]);

  return (
    <div className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6">
      <div className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className="grid size-7 place-items-center rounded-[7px] bg-[var(--color-accent)] text-xs font-bold text-[var(--color-accent-contrast)]"
        >
          V
        </span>
        <span className="font-semibold">Verity</span>
      </div>

      {state === "checking" ? (
        <div role="status" aria-live="polite" className="space-y-2">
          <h1 className="text-2xl">Confirming your email…</h1>
          <p className="text-[var(--color-text-secondary)]">This only takes a moment.</p>
        </div>
      ) : null}

      {state === "verified" ? (
        <div className="space-y-4">
          <h1 className="text-2xl">You&apos;re all set</h1>
          <p className="text-[var(--color-text-secondary)]">
            Your email is confirmed. Sign in and add your resume — everything else in Verity is
            built on it.
          </p>
          <Button href="/login">Sign in</Button>
        </div>
      ) : null}

      {state === "failed" ? (
        <div className="space-y-4">
          <h1 className="text-2xl">That link didn&apos;t work</h1>
          <p className="text-[var(--color-text-secondary)]">{message}</p>
          <p className="text-sm text-[var(--color-text-secondary)]">
            If you already confirmed this address, just sign in — the link only works once.
          </p>
          <Button href="/login">Go to sign in</Button>
        </div>
      ) : null}

      {state === "missing" ? (
        <div className="space-y-4">
          <h1 className="text-2xl">Nothing to confirm</h1>
          <p className="text-[var(--color-text-secondary)]">
            This page needs the link from your confirmation email.
          </p>
          <Button href="/login">Go to sign in</Button>
        </div>
      ) : null}

      <p className="text-sm text-[var(--color-text-muted)]">
        Need help?{" "}
        <Link href="/login" className="text-[var(--color-accent)] hover:underline">
          Back to Verity
        </Link>
      </p>
    </div>
  );
}

export default function VerifyEmailPage() {
  return (
    <Suspense fallback={<div className="p-10">Loading…</div>}>
      <Verify />
    </Suspense>
  );
}
