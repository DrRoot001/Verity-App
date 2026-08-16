"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardBody } from "@/components/ui/card";
import { login, signup } from "@/features/auth/api";
import { bootstrapSession, storeSession } from "@/features/auth/session";
import { ApiError, NetworkError } from "@/lib/api/client";

type Mode = "signin" | "signup";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; requestId: string | null } | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(bootstrapSession, []);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setFieldErrors({});
    setNotice(null);

    try {
      if (mode === "signup") {
        const result = await signup(email, password);
        setNotice(result.message);
        setMode("signin");
      } else {
        storeSession(await login(email, password));
        router.push("/dashboard");
      }
    } catch (caught) {
      if (caught instanceof ApiError) {
        setFieldErrors(caught.fieldErrors);
        setError({ message: caught.message, requestId: caught.requestId });
      } else if (caught instanceof NetworkError) {
        setError({ message: caught.message, requestId: null });
      } else {
        throw caught;
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main id="main" className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
      <div className="mb-8 space-y-1">
        <p className="text-xl font-semibold tracking-tight text-[var(--color-accent)]">Verity</p>
        <p className="text-[var(--color-text-secondary)]">
          One workspace per opportunity. Enter your context once.
        </p>
      </div>

      <Card>
        <CardBody>
          <form onSubmit={onSubmit} className="space-y-4" noValidate>
            <div className="space-y-1.5">
              <label htmlFor="email" className="block text-sm font-medium">
                Email
              </label>
              <input
                id="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                aria-invalid={Boolean(fieldErrors.email)}
                aria-describedby={fieldErrors.email ? "email-error" : undefined}
                className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3"
              />
              {fieldErrors.email ? (
                <p id="email-error" className="text-xs text-[var(--color-critical)]">
                  {fieldErrors.email}
                </p>
              ) : null}
            </div>

            <div className="space-y-1.5">
              <label htmlFor="password" className="block text-sm font-medium">
                Password
              </label>
              <input
                id="password"
                type="password"
                autoComplete={mode === "signup" ? "new-password" : "current-password"}
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-invalid={Boolean(fieldErrors.password)}
                aria-describedby={fieldErrors.password ? "password-error" : "password-hint"}
                className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3"
              />
              {fieldErrors.password ? (
                <p id="password-error" className="text-xs text-[var(--color-critical)]">
                  {fieldErrors.password}
                </p>
              ) : mode === "signup" ? (
                <p id="password-hint" className="text-xs text-[var(--color-text-muted)]">
                  At least 10 characters. Length beats symbols.
                </p>
              ) : null}
            </div>

            {error ? (
              <div
                role="alert"
                className="rounded-[var(--radius-control)] border border-[var(--color-critical)] bg-[var(--color-critical-quiet)] px-3 py-2 text-sm"
              >
                <p>{error.message}</p>
                {error.requestId ? (
                  <p className="mt-1 text-xs text-[var(--color-text-muted)]">
                    Reference: <code className="font-[var(--font-mono)]">{error.requestId}</code>
                  </p>
                ) : null}
              </div>
            ) : null}

            {notice ? (
              <div
                role="status"
                className="rounded-[var(--radius-control)] border border-[var(--color-info)] bg-[var(--color-info-quiet)] px-3 py-2 text-sm"
              >
                {notice}
              </div>
            ) : null}

            <Button type="submit" size="lg" loading={busy}>
              {mode === "signin" ? "Sign in" : "Create account"}
            </Button>
          </form>
        </CardBody>
      </Card>

      <p className="mt-4 text-center text-sm text-[var(--color-text-secondary)]">
        {mode === "signin" ? "No account yet?" : "Already have an account?"}{" "}
        <button
          type="button"
          onClick={() => {
            setMode(mode === "signin" ? "signup" : "signin");
            setError(null);
            setFieldErrors({});
          }}
          className="font-medium text-[var(--color-accent)] underline underline-offset-2"
        >
          {mode === "signin" ? "Create one" : "Sign in"}
        </button>
      </p>
    </main>
  );
}
