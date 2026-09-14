"use client";

/**
 * Password reset request (PRD §10.2).
 *
 * The response is deliberately identical whether or not the address exists.
 * Telling an anonymous visitor "no account with that email" turns this form
 * into an account-enumeration oracle, which is the same reason login answers
 * with one generic message.
 */

import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { requestPasswordReset } from "@/features/auth/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await requestPasswordReset(email);
    } catch {
      // Swallowed on purpose: a failure here must not distinguish a real
      // address from an unknown one.
    } finally {
      setBusy(false);
      setSent(true);
    }
  }

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

      {sent ? (
        <div className="space-y-4">
          <h1 className="text-2xl">Check your email</h1>
          <p className="text-[var(--color-text-secondary)]">
            If an account exists for <span className="font-medium">{email}</span>, a reset link
            is on its way. It expires in 30 minutes.
          </p>
          <Button href="/login" variant="secondary">
            Back to sign in
          </Button>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-5">
          <div className="space-y-2">
            <h1 className="text-2xl">Reset your password</h1>
            <p className="text-[var(--color-text-secondary)]">
              We&apos;ll email you a link to set a new one.
            </p>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="email" className="text-sm font-medium">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              autoFocus
              autoComplete="email"
              className="field"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </div>

          <Button type="submit" fullWidth loading={busy} disabled={!email.trim()}>
            Send reset link
          </Button>

          <p className="text-center text-sm text-[var(--color-text-secondary)]">
            Remembered it?{" "}
            <Link href="/login" className="text-[var(--color-accent)] hover:underline">
              Sign in
            </Link>
          </p>
        </form>
      )}
    </div>
  );
}
