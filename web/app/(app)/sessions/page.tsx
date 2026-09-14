"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, PageHeader } from "@/components/ui/card";
import { Empty, Loading } from "@/components/ui/states";
import {
  createLiveSession,
  createMockSession,
  listLiveSessions,
  listMockSessions,
  type LiveSession,
  type MockSession,
} from "@/features/sessions/api";
import {
  ReadinessList,
  blocked,
  readinessChecks,
  type ReadinessCheck,
} from "@/features/sessions/readiness";
import { getContext, listWorkspaces } from "@/features/workspace/api";
import type { WorkspaceSummary } from "@/lib/api/types";

export default function SessionsPage() {
  const router = useRouter();
  const query = useSearchParams();
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[] | null>(null);
  const [mocks, setMocks] = useState<MockSession[]>([]);
  const [live, setLive] = useState<LiveSession[]>([]);
  const [workspace, setWorkspace] = useState(query.get("workspace") ?? "");
  const [mode, setMode] = useState("behavioral");
  const [busy, setBusy] = useState<"mock" | "live" | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Both modes read one ContextBundle; a thin bundle is why sessions feel
  // generic, so the gate is checked here rather than discovered mid-interview.
  const [checks, setChecks] = useState<ReadinessCheck[] | null>(null);
  useEffect(() => {
    void Promise.all([listWorkspaces(), listMockSessions(), listLiveSessions()])
      .then(([w, m, l]) => {
        setWorkspaces(w);
        setMocks(m);
        setLive(l);
        if (!workspace && w[0]) setWorkspace(w[0].id);
      })
      .catch(() => setError("We couldn't load interview data."));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!workspace) return;
    setChecks(null);
    void getContext(workspace)
      .then((bundle) => setChecks(readinessChecks(bundle, workspace)))
      .catch(() => setChecks(null));
  }, [workspace]);

  const notReady = checks !== null && blocked(checks);

  async function launch(kind: "mock" | "live") {
    if (!workspace) return;
    setBusy(kind);
    setError(null);
    try {
      if (kind === "mock") {
        const s = await createMockSession({
          workspace_id: workspace,
          mode,
          persona: "neutral_evaluator",
          difficulty: 3,
          duration_minutes: 20,
          live_feedback: false,
        });
        router.push(`/sessions/mock/${s.id}`);
      } else {
        const s = await createLiveSession({
          workspace_id: workspace,
          interview_type: mode,
          response_mode: "balanced",
        });
        router.push(`/sessions/live/${s.id}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start interview");
      setBusy(null);
    }
  }
  const name = (id: string) => {
    const w = workspaces?.find((x) => x.id === id);
    return w ? `${w.role_title} · ${w.company_name}` : "Workspace";
  };
  return (
    <div className="space-y-8">
      <PageHeader
        title="Interviews"
        description="Practise out loud with an AI interviewer, then let the copilot listen during the real conversation."
      />
      {workspaces && workspaces.length > 0 ? (
        <Card>
          <CardHeader
            title="Start an interview"
            meta="Both modes use the same approved workspace evidence."
          />
          <CardBody>
            <div className="grid gap-4 md:grid-cols-[1fr_1fr_auto_auto] md:items-end">
              <label className="space-y-1.5 text-sm">
                <span className="font-medium">Workspace</span>
                <select
                  className="field"
                  value={workspace}
                  onChange={(e) => setWorkspace(e.target.value)}
                >
                  {workspaces?.map((w) => (
                    <option value={w.id} key={w.id}>
                      {name(w.id)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="space-y-1.5 text-sm">
                <span className="font-medium">Interview type</span>
                <select
                  className="field"
                  value={mode}
                  onChange={(e) => setMode(e.target.value)}
                >
                  {[
                    "behavioral",
                    "technical",
                    "hiring_manager",
                    "recruiter_screen",
                    "system_design",
                    "executive",
                  ].map((x) => (
                    <option value={x} key={x}>
                      {x.replaceAll("_", " ")}
                    </option>
                  ))}
                </select>
              </label>
              <Button
                loading={busy === "mock"}
                onClick={() => void launch("mock")}
                disabled={!workspace || notReady}
              >
                Start mock
              </Button>
              <Button
                variant="secondary"
                loading={busy === "live"}
                onClick={() => void launch("live")}
                disabled={!workspace || notReady}
              >
                Launch live
              </Button>
            </div>
            {checks ? (
              <div className="mt-5 border-t border-[var(--color-border-subtle)] pt-4">
                <p className="pb-3 text-xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)]">
                  {notReady ? "Finish these before you start" : "What this session can draw on"}
                </p>
                <ReadinessList checks={checks} />
                {notReady ? (
                  <p className="pt-3 text-sm text-[var(--color-text-secondary)]">
                    An interview with no approved history can only produce generic advice, so it
                    is not worth your time yet.
                  </p>
                ) : null}
              </div>
            ) : null}
            {error ? (
              <p role="alert" className="mt-3 text-sm text-[var(--color-critical)]">
                {error}
              </p>
            ) : null}
          </CardBody>
        </Card>
      ) : null}
      {!workspaces ? (
        <Loading />
      ) : workspaces.length === 0 ? (
        <Empty
          title="Create a workspace first"
          description="An interview needs a role and company context."
          action={{ label: "Create workspace", href: "/workspaces" }}
        />
      ) : (
        <section className="space-y-3">
          <h2 className="text-lg">Recent sessions</h2>
          <div className="grid gap-3 lg:grid-cols-2">
            {[
              ...live.map((s) => ({ ...s, kind: "live" as const })),
              ...mocks.map((s) => ({ ...s, kind: "mock" as const })),
            ].length === 0 ? (
              <Empty
                title="No interviews yet"
                description="Choose a workspace above to run your first mock."
              />
            ) : (
              [
                ...live.map((s) => ({ ...s, kind: "live" as const })),
                ...mocks.map((s) => ({ ...s, kind: "mock" as const })),
              ].map((s) => (
                <Card key={`${s.kind}-${s.id}`}>
                  <CardBody>
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="font-medium">{name(s.workspace_id)}</p>
                        <p className="mt-1 text-sm capitalize text-[var(--color-text-secondary)]">
                          {s.kind} ·{" "}
                          {("mode" in s ? s.mode : s.interview_type).replaceAll("_", " ")}
                        </p>
                      </div>
                      <Badge
                        tone={
                          s.status === "completed"
                            ? "positive"
                            : s.status === "active"
                              ? "accent"
                              : "neutral"
                        }
                      >
                        {s.status}
                      </Badge>
                    </div>
                    <div className="mt-4">
                      <Button
                        size="sm"
                        variant="secondary"
                        href={
                          s.status === "completed"
                            ? `/sessions/${s.kind}/${s.id}/report`
                            : `/sessions/${s.kind}/${s.id}`
                        }
                      >
                        {s.status === "completed" ? "View report" : "Continue"}
                      </Button>
                    </div>
                  </CardBody>
                </Card>
              ))
            )}
          </div>
        </section>
      )}
    </div>
  );
}
