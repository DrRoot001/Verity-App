"use client";
import { use, useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { PageHeader } from "@/components/ui/card";
import { ErrorState, Loading } from "@/components/ui/states";
import { whoami, type StaffPrincipal } from "@/features/admin/api";
import { DiagnosticsPane } from "@/features/admin/diagnostics";
import {
  AISettingsPane,
  FlagsPane,
  PromptsPane,
  SessionsPane,
  StaffPane,
} from "@/features/admin/operations";
import { AuditPane, UsersPane } from "@/features/admin/panes";
export default function AdminSection({ params }: { params: Promise<{ section: string }> }) {
  const { section } = use(params);
  const [p, setP] = useState<StaffPrincipal | null>(null);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    void whoami()
      .then(setP)
      .catch(() => setMissing(true));
  }, []);
  if (missing)
    return (
      <ErrorState
        title="Page not found"
        description="This operations route is unavailable for this account."
      />
    );
  if (!p) return <Loading />;
  const can = (x: string) => p.permissions.includes(x);
  const allowed: Record<string, string> = {
    users: "user.read",
    sessions: "session.read_metadata",
    audit: "audit.read",
    staff: "staff.read",
    prompts: "prompts.read",
    flags: "flags.read",
    settings: "ai.read",
  };
  const requiredPermission = allowed[section];
  if (!requiredPermission) {
    return (
      <ErrorState title="Page not found" description="This operations route does not exist." />
    );
  }
  if (!can(requiredPermission))
    return (
      <ErrorState
        title="Access restricted"
        description={`Your ${p.roles.join(", ")} role does not include this operation.`}
      />
    );
  return (
    <div className="space-y-7">
      <PageHeader
        title={TITLES[section] ?? "Operations"}
        description="Least-privilege staff workspace. Privileged actions are audited."
      >
        <div className="flex gap-2">
          {p.roles.map((r) => (
            <Badge key={r} tone="accent">
              {r}
            </Badge>
          ))}
        </div>
      </PageHeader>
      {section === "users" ? (
        <UsersPane canSuspend={can("user.suspend")} canDelete={can("user.delete")} />
      ) : section === "sessions" ? (
        <div className="space-y-4">
          <SessionsPane />
          <DiagnosticsPane />
        </div>
      ) : section === "audit" ? (
        <AuditPane />
      ) : section === "staff" ? (
        <StaffPane canWrite={can("staff.write")} />
      ) : section === "prompts" ? (
        <PromptsPane canWrite={can("prompts.write")} />
      ) : section === "flags" ? (
        <FlagsPane canWrite={can("flags.write")} />
      ) : (
        <AISettingsPane canWrite={can("ai.write")} />
      )}
    </div>
  );
}

const TITLES: Record<string, string> = {
  users: "User operations",
  sessions: "Session operations",
  audit: "Audit log",
  staff: "Staff and roles",
  prompts: "Prompt registry",
  flags: "Feature flags",
  settings: "AI and platform settings",
};
