"use client";

/**
 * Password reset confirmation (PRD §10.2).
 *
 * Completing a reset signs out every other session server-side, so the page
 * says that before the user commits rather than letting them discover it when
 * their phone logs itself out.
 */

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { Button } from "@/components/ui/button";
import { confirmPasswordReset } from "@/features/auth/api";
import { ApiError } from "@/lib/api/client";

function Reset() {
  const token = useSearchParams().get("token");
  const [done, setDone] = useState(false);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const mismatch = confirm.length > 0 && password !== confirm;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!token || mismatch) return;

    setBusy(true);
    setError(null);
    try {
      await confirmPasswordReset(token, password);
      setDone(true);
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "We couldn't reset your password. The link may have expired.",
      );
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-4 px-6">
        <h1 className="text-2xl">Password updated</h1>
        <p className="text-[var(--color-text-secondary)]">
          Every other session has been signed out. Sign in with your new password.
        </p>
        <Button href="/login">Sign in</Button>
      </div>
    );
  }

  if (!token) {
    return (
      <div className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-4 px-6">
        <h1 className="text-2xl">This link is incomplete</h1>
        <p className="text-[var(--color-text-secondary)]">
          Request a new reset link and use the button in the email.
        </p>
        <Button href="/forgot-password">Request a new link</Button>
      </div>
    );
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

      <form onSubmit={submit} className="space-y-5">
        <div className="space-y-2">
          <h1 className="text-2xl">Choose a new password</h1>
          <p className="text-[var(--color-text-secondary)]">
            Setting it signs you out everywhere else.
          </p>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="password" className="text-sm font-medium">
            New password
          </label>
          <input
            id="password"
            type="password"
            required
            autoFocus
            autoComplete="new-password"
            className="field"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="confirm" className="text-sm font-medium">
            Confirm password
          </label>
          <input
            id="confirm"
            type="password"
            required
            autoComplete="new-password"
            className="field"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
          />
          {mismatch ? (
            <p className="text-sm text-[var(--color-critical)]">These don&apos;t match.</p>
          ) : null}
        </div>

        {error ? (
          <p role="alert" className="text-sm text-[var(--color-critical)]">
            {error}
          </p>
        ) : null}

        <Button
          type="submit"
          fullWidth
          loading={busy}
          disabled={!password || mismatch}
        >
          Set new password
        </Button>

        <p className="text-center text-sm text-[var(--color-text-secondary)]">
          <Link href="/login" className="text-[var(--color-accent)] hover:underline">
            Back to sign in
          </Link>
        </p>
      </form>
    </div>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<div className="p-10">Loading…</div>}>
      <Reset />
    </Suspense>
  );
}
