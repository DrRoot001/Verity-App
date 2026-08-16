"use client";

/** Workspace list and creation (PRD §11, FR-WS-001). */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Empty, Loading } from "@/components/ui/states";
import { createWorkspace, listWorkspaces } from "@/features/workspace/api";
import { ApiError } from "@/lib/api/client";
import type { WorkspaceSummary } from "@/lib/api/types";

export default function WorkspacesPage() {
  const router = useRouter();
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [creating, setCreating] = useState(false);
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");
  const [jd, setJd] = useState("");

  const load = useCallback(async () => {
    try {
      setWorkspaces(await listWorkspaces());
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
      else throw caught;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onCreate(event: React.FormEvent) {
    event.preventDefault();
    setCreating(true);
    setError(null);
    try {
      const created = await createWorkspace({
        company_name: company,
        role_title: role,
        ...(jd.trim().length >= 80 ? { jd_text: jd } : {}),
      });
      router.push(`/workspaces/${created.id}`);
    } catch (caught) {
      if (caught instanceof ApiError) setError(caught);
      else throw caught;
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-8">
      <h1 className="text-2xl">Workspaces</h1>

      <Card>
        <CardHeader
          title="New workspace"
          meta="Only the company and role are required — everything else can come later."
        />
        <CardBody>
          <form onSubmit={onCreate} className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <label htmlFor="company" className="block text-sm font-medium">
                  Company
                </label>
                <input
                  id="company"
                  required
                  value={company}
                  onChange={(e) => setCompany(e.target.value)}
                  className="field"
                />
              </div>
              <div className="space-y-1.5">
                <label htmlFor="role" className="block text-sm font-medium">
                  Role
                </label>
                <input
                  id="role"
                  required
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  className="field"
                />
              </div>
            </div>

            <div className="space-y-1.5">
              <label htmlFor="jd" className="block text-sm font-medium">
                Job description <span className="text-[var(--color-text-muted)]">(optional)</span>
              </label>
              <textarea
                id="jd"
                rows={6}
                value={jd}
                onChange={(e) => setJd(e.target.value)}
                aria-describedby="jd-hint"
                placeholder="Paste the posting to get a requirement-by-requirement match."
                className="field text-sm"
              />
              <p id="jd-hint" className="text-xs text-[var(--color-text-muted)]">
                Without a job description, Verity marks the opportunity as inferred rather than
                guessing a match score.
              </p>
            </div>

            {error ? (
              <p role="alert" className="text-sm text-[var(--color-critical)]">
                {error.message}
              </p>
            ) : null}

            <Button type="submit" loading={creating} disabled={!company || !role}>
              Create workspace
            </Button>
          </form>
        </CardBody>
      </Card>

      {!workspaces ? (
        <Loading rows={2} label="Loading workspaces" />
      ) : workspaces.length === 0 ? (
        <Empty
          title="No workspaces yet"
          description="Each workspace is one opportunity. Its context is shared by preparation, mock interviews and live assistance."
        />
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2">
          {workspaces.map((w) => (
            <li key={w.id}>
              <Card as="article" interactive>
                <CardBody>
                  <div className="flex items-start justify-between gap-3">
                    <div className="space-y-0.5">
                      <p className="font-medium">{w.role_title}</p>
                      <p className="text-sm text-[var(--color-text-secondary)]">{w.company_name}</p>
                      <p className="text-xs text-[var(--color-text-muted)]">
                        {w.stage.replace(/_/g, " ")} · round {w.round_index}
                      </p>
                    </div>
                    <Badge tone={w.live_copilot_allowed ? "accent" : "warning"}>
                      {w.live_copilot_allowed ? "assisted" : "proctored"}
                    </Badge>
                  </div>
                  <div className="pt-3">
                    <Button size="sm" variant="secondary" href={`/workspaces/${w.id}`}>
                      Open workspace
                    </Button>
                  </div>
                </CardBody>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
