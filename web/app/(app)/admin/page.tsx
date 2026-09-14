"use client";

/**
 * Internal operations panel (PRD §18, Phase 13).
 *
 * The gate for this phase is that an on-call engineer can diagnose a failed
 * session end-to-end from here alone, so the session pane shows per-stage
 * latencies and grounding outcomes rather than a status pill.
 *
 * A non-staff account never reaches this page: the API answers 404, and the
 * page renders as if it does not exist rather than announcing that permission
 * was denied.
 */

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { PageHeader } from "@/components/ui/card";
import { ErrorState, Loading } from "@/components/ui/states";
import { whoami, type StaffPrincipal } from "@/features/admin/api";
import { DiagnosticsPane } from "@/features/admin/diagnostics";
import { AuditPane, MetricsPane, UsersPane } from "@/features/admin/panes";
import { ApiError } from "@/lib/api/client";

export default function AdminPage() {
  const [principal, setPrincipal] = useState<StaffPrincipal | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        setPrincipal(await whoami());
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 404) setNotFound(true);
        else if (caught instanceof ApiError) setError(caught);
        else throw caught;
      }
    })();
  }, []);

  if (notFound) {
    return (
      <ErrorState
        title="Page not found"
        description="This page doesn't exist, or you don't have access to it."
        action={{ label: "Back to dashboard", href: "/dashboard" }}
      />
    );
  }

  if (error) {
    return (
      <ErrorState
        title="We couldn't load the operations panel"
        description={error.message}
        requestId={error.requestId}
      />
    );
  }

  if (!principal) return <Loading rows={3} label="Checking your access" />;

  const can = (permission: string) => principal.permissions.includes(permission);

  return (
    <div className="space-y-8">
      <PageHeader
        title="Operations"
        description="Internal tooling. Every privileged action here is recorded against your account."
      >
        <div className="flex flex-wrap gap-1.5 pt-1">
          {principal.roles.map((role) => (
            <Badge key={role} tone="accent">
              {role}
            </Badge>
          ))}
        </div>
      </PageHeader>

      {can("metrics.read") ? <MetricsPane /> : null}
      {can("user.read") ? (
        <UsersPane canSuspend={can("user.suspend")} canDelete={can("user.delete")} />
      ) : null}
      {can("session.read_metadata") ? <DiagnosticsPane /> : null}
      {can("audit.read") ? <AuditPane /> : null}
    </div>
  );
}
