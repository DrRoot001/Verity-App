"use client";
/* eslint-disable max-lines */

/** Operations panel panes (PRD §18). Split from the page to keep each file
 * reviewable; the page composes them behind permission checks. */

import { useCallback, useEffect, useState } from "react";

import { Badge, Stat } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Loading } from "@/components/ui/states";
import {
  auditLog,
  cancelUserDeletion,
  metrics as fetchMetrics,
  restoreUser,
  scheduleUserDeletion,
  searchUsers,
  suspendUser,
  type AdminMetrics,
  type AdminUser,
  type AuditEntry,
} from "@/features/admin/api";
import { ApiError } from "@/lib/api/client";

export function MetricsPane() {
  const [data, setData] = useState<AdminMetrics | null>(null);

  useEffect(() => {
    void fetchMetrics()
      .then(setData)
      .catch(() => setData(null));
  }, []);

  return (
    <Card>
      <CardHeader
        title="Platform"
        meta="Activation is the only number that moves when the loop works."
      />
      <CardBody>
        {data ? (
          <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
            <Stat value={String(data.users_total)} label="Users" />
            <Stat value={String(data.activated_users)} label="Activated" tone="accent" />
            <Stat value={String(data.workspaces_total)} label="Workspaces" />
            <Stat value={String(data.live_sessions_total)} label="Live sessions" />
            <Stat value={String(data.mock_sessions_total)} label="Mock sessions" />
            <Stat value={String(data.reports_total)} label="Reports" />
            <Stat value={String(data.stories_approved)} label="Approved stories" />
            <Stat value={String(data.users_active_7d)} label="Active 7d" />
          </div>
        ) : (
          <Loading rows={2} label="Loading metrics" />
        )}
      </CardBody>
    </Card>
  );
}

export function UsersPane({
  canSuspend,
  canDelete = false,
}: {
  canSuspend: boolean;
  canDelete?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [selected, setSelected] = useState<AdminUser | null>(null);
  const [action, setAction] = useState<"suspend" | "restore" | "delete">("suspend");
  const [reason, setReason] = useState("");
  const [caseId, setCaseId] = useState("");
  const [confirmEmail, setConfirmEmail] = useState("");

  const run = useCallback(async () => {
    setBusy(true);
    try {
      setUsers(await searchUsers(query));
      setMessage(null);
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Search failed");
    } finally {
      setBusy(false);
    }
  }, [query]);

  useEffect(() => {
    void run();
    // Initial load only; subsequent searches are explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function changeStatus() {
    if (!selected || reason.trim().length < 8 || !caseId.trim()) return;
    setBusy(true);
    try {
      if (action === "delete") {
        await scheduleUserDeletion(
          selected.id,
          confirmEmail.trim(),
          reason.trim(),
          caseId.trim(),
        );
      } else if (selected.status === "pending_deletion") {
        await cancelUserDeletion(selected.id, reason.trim(), caseId.trim());
      } else if (action === "suspend") {
        await suspendUser(selected.id, reason.trim(), caseId.trim());
      } else {
        await restoreUser(selected.id, reason.trim(), caseId.trim());
      }
      setMessage(`${selected.email} updated. The action is in the audit log.`);
      setSelected(null);
      setReason("");
      setCaseId("");
      setConfirmEmail("");
      await run();
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Account update failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader
        title="Users"
        meta="Account state only — interview content is not readable from here."
      />
      <CardBody>
        <div className="flex gap-2 pb-4">
          <input
            className="field flex-1"
            placeholder="Search by email"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void run();
            }}
          />
          <Button size="sm" onClick={() => void run()} loading={busy}>
            Search
          </Button>
        </div>

        {message ? (
          <p role="status" className="pb-3 text-sm text-[var(--color-text-secondary)]">
            {message}
          </p>
        ) : null}

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-[var(--color-text-secondary)]">
              <tr>
                <th className="pb-2 font-medium">Email</th>
                <th className="pb-2 font-medium">Status</th>
                <th className="pb-2 text-right font-medium">Workspaces</th>
                <th className="pb-2 text-right font-medium">Sessions</th>
                {canSuspend || canDelete ? <th className="pb-2" /> : null}
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id} className="border-t border-[var(--color-border-subtle)]">
                  <td className="py-2 pr-3">{user.email}</td>
                  <td className="py-2 pr-3">
                    <Badge tone={user.status === "active" ? "positive" : "warning"}>
                      {user.status}
                    </Badge>
                  </td>
                  <td className="numeric py-2 text-right">{user.workspaces}</td>
                  <td className="numeric py-2 text-right">
                    {user.mock_sessions + user.live_sessions}
                  </td>
                  {canSuspend || canDelete ? (
                    <td className="py-2 pl-3 text-right">
                      <div className="flex justify-end gap-1">
                        {canSuspend ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => {
                              setSelected(user);
                              setAction(user.status === "active" ? "suspend" : "restore");
                            }}
                          >
                            {user.status === "active" ? "Suspend" : "Restore"}
                          </Button>
                        ) : null}
                        {canDelete && user.status !== "pending_deletion" ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => {
                              setSelected(user);
                              setAction("delete");
                            }}
                          >
                            Delete
                          </Button>
                        ) : null}
                      </div>
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {selected ? (
          <div className="mt-4 rounded-xl border border-[var(--color-border-strong)] bg-[var(--color-raised)] p-4">
            <p className="font-medium">
              {action === "delete"
                ? "Schedule deletion for"
                : action === "suspend"
                  ? "Suspend"
                  : "Restore"}{" "}
              {selected.email}
            </p>
            <p className="mt-1 text-xs text-[var(--color-text-secondary)]">
              {action === "delete"
                ? "Deletion starts after the recovery grace period. Type-to-confirm is bound to this exact account."
                : "This action revokes active sessions and is permanently recorded."}
            </p>
            <div className="mt-3 grid gap-3 md:grid-cols-[1fr_12rem_auto] md:items-end">
              <label className="space-y-1 text-sm">
                <span>Reason</span>
                <input
                  className="field"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              {action === "delete" ? (
                <label className="space-y-1 text-sm md:col-span-2">
                  <span>Type {selected.email} to confirm</span>
                  <input
                    className="field"
                    value={confirmEmail}
                    onChange={(e) => setConfirmEmail(e.target.value)}
                  />
                </label>
              ) : null}
              <label className="space-y-1 text-sm">
                <span>Support case</span>
                <input
                  className="field"
                  value={caseId}
                  onChange={(e) => setCaseId(e.target.value)}
                />
              </label>
              <div className="flex gap-2">
                <Button variant="secondary" size="sm" onClick={() => setSelected(null)}>
                  Cancel
                </Button>
                <Button
                  variant={action === "suspend" || action === "delete" ? "danger" : "primary"}
                  size="sm"
                  loading={busy}
                  disabled={
                    reason.trim().length < 8 ||
                    !caseId.trim() ||
                    (action === "delete" && confirmEmail.trim() !== selected.email)
                  }
                  onClick={() => void changeStatus()}
                >
                  Confirm
                </Button>
              </div>
            </div>
          </div>
        ) : null}
      </CardBody>
      <CardFooter>{users.length} account(s) shown</CardFooter>
    </Card>
  );
}

export function AuditPane() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);

  useEffect(() => {
    void auditLog()
      .then(setEntries)
      .catch(() => setEntries([]));
  }, []);

  return (
    <Card>
      <CardHeader
        title="Audit"
        meta="Append-only. Reason and case are required for privileged actions."
      />
      <CardBody>
        {entries.length === 0 ? (
          <p className="text-sm text-[var(--color-text-secondary)]">No recorded actions yet.</p>
        ) : (
          <ul className="space-y-3">
            {entries.map((entry) => (
              <li
                key={entry.id}
                className="border-b border-[var(--color-border-subtle)] pb-3 text-sm last:border-0"
              >
                <div className="flex flex-wrap items-baseline gap-2">
                  <code className="text-xs">{entry.action}</code>
                  <span className="text-xs text-[var(--color-text-secondary)]">
                    {new Date(entry.created_at).toLocaleString()}
                  </span>
                  {entry.case_id ? <Badge tone="neutral">{entry.case_id}</Badge> : null}
                </div>
                {entry.reason ? (
                  <p className="pt-1 text-[var(--color-text-secondary)]">{entry.reason}</p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}
