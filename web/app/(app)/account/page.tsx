"use client";

/**
 * Account settings (PRD §10.3, §29.4).
 *
 * Deletion is the only destructive action in the product, so it is separated
 * from everything else, states its consequences before asking, requires the
 * user to type their own address, and shows the cancel path for as long as the
 * grace period runs.
 */

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, PageHeader } from "@/components/ui/card";
import {
  cancelDeletion,
  exportAccount,
  requestDeletion,
  type DeletionState,
} from "@/features/account/api";
import { currentUser } from "@/features/auth/session";
import { ApiError } from "@/lib/api/client";

const ERASED = [
  "Your resume, work history, skills and stories",
  "Every workspace, job description and match analysis",
  "All interview sessions, transcripts and reports",
  "Your preparation plans and readiness history",
];

export default function AccountPage() {
  const email = currentUser()?.email ?? "";
  const [pending, setPending] = useState<DeletionState | null>(null);
  const [confirm, setConfirm] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function download() {
    setBusy(true);
    try {
      const data = await exportAccount();
      // The browser download is the delivery mechanism; the API is the source.
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = "verity-account-export.json";
      link.click();
      URL.revokeObjectURL(url);
      setMessage("Your export has downloaded.");
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Export failed");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      setPending(await requestDeletion(confirm));
      setMessage(null);
      setConfirm("");
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  async function undo() {
    setBusy(true);
    try {
      await cancelDeletion();
      setPending(null);
      setMessage("Deletion cancelled. Your account is active.");
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Cancel failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-8">
      <PageHeader title="Account" description={email} />

      {pending ? (
        <Card>
          <CardHeader
            title="Deletion scheduled"
            meta={`Your account and all of its data will be permanently erased after ${new Date(
              pending.execute_after,
            ).toLocaleDateString()}. You can still cancel.`}
            action={
              <Button size="sm" onClick={() => void undo()} loading={busy}>
                Cancel deletion
              </Button>
            }
          />
        </Card>
      ) : null}

      <Card>
        <CardHeader
          title="Export your data"
          meta="Everything we hold about you, as a single JSON file."
          action={
            <Button
              size="sm"
              variant="secondary"
              onClick={() => void download()}
              loading={busy}
            >
              Download
            </Button>
          }
        />
      </Card>

      {message ? (
        <p role="status" className="text-sm text-[var(--color-text-secondary)]">
          {message}
        </p>
      ) : null}

      {pending ? null : (
        <Card>
          <CardHeader title="Delete your account" />
          <CardBody>
            <div className="space-y-4">
              <div className="space-y-2">
                <Badge tone="critical">Permanent</Badge>
                <p className="text-sm text-[var(--color-text-secondary)]">
                  These are erased and cannot be recovered:
                </p>
                <ul className="list-disc space-y-1 pl-5 text-sm text-[var(--color-text-secondary)]">
                  {ERASED.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
                <p className="text-sm text-[var(--color-text-secondary)]">
                  Nothing happens straight away — you have a grace period to change your mind.
                </p>
              </div>

              <div className="space-y-2">
                <label htmlFor="confirm-email" className="block text-sm font-medium">
                  Type <span className="font-mono">{email}</span> to confirm
                </label>
                <input
                  id="confirm-email"
                  className="field w-full max-w-md"
                  value={confirm}
                  onChange={(event) => setConfirm(event.target.value)}
                  autoComplete="off"
                />
              </div>

              <Button
                variant="danger"
                onClick={() => void remove()}
                disabled={confirm.trim().toLowerCase() !== email.toLowerCase()}
                loading={busy}
              >
                Delete my account
              </Button>
            </div>
          </CardBody>
        </Card>
      )}
    </div>
  );
}
