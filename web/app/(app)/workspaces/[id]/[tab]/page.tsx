"use client";
import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, PageHeader } from "@/components/ui/card";
import { Loading } from "@/components/ui/states";
import { WorkspaceNav } from "@/features/workspace/nav";
import {
  archiveWorkspace,
  attachJobDescription,
  getContext,
  updateWorkspace,
} from "@/features/workspace/api";
import type { ContextBundle } from "@/lib/api/types";
export default function WorkspaceTab({
  params,
}: {
  params: Promise<{ id: string; tab: string }>;
}) {
  const { id, tab } = use(params);
  const router = useRouter();
  const [ctx, setCtx] = useState<ContextBundle | null>(null);
  const [date, setDate] = useState("");
  const [integrity, setIntegrity] = useState("assisted");
  const [message, setMessage] = useState<string | null>(null);
  const [jd, setJd] = useState("");
  const [savingJd, setSavingJd] = useState(false);
  useEffect(() => {
    void getContext(id).then((c) => {
      setCtx(c);
      setDate(c.opportunity.interview_at?.slice(0, 16) ?? "");
      setIntegrity(c.opportunity.integrity_mode);
    });
  }, [id]);
  if (!ctx) return <Loading />;
  const launch = (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader
          title="Mock interview"
          meta="Practice with an adaptive interviewer and receive a structured report."
        />
        <CardBody>
          <Button href={`/sessions?workspace=${id}`}>Configure mock</Button>
        </CardBody>
      </Card>
      <Card>
        <CardHeader
          title="Live copilot"
          meta={
            integrity === "proctored"
              ? "Disabled for this proctored workspace."
              : "Listens to the interviewer and shows grounded guidance as questions come up."
          }
        />
        <CardBody>
          <Button
            variant="secondary"
            href={`/sessions?workspace=${id}`}
            disabled={integrity === "proctored"}
          >
            Launch live
          </Button>
        </CardBody>
      </Card>
    </div>
  );
  return (
    <div className="space-y-7">
      <WorkspaceNav id={id} />
      <PageHeader
        title={`${ctx.opportunity.role_title} · ${ctx.opportunity.company_name}`}
        description={
          tab === "job"
            ? "Role requirements and inferred interview themes."
            : tab === "settings"
              ? "Opportunity-specific interview settings."
              : "Interview workspace"
        }
      />
      {tab === "job" ? (
        <div className="space-y-4">
          <Card>
            <CardHeader
              title={
                ctx.opportunity.jd_present
                  ? "Replace the job description"
                  : "Add the job description"
              }
              meta={
                ctx.opportunity.jd_present
                  ? "Pasting a new one re-parses the requirements and recomputes your match."
                  : "Without it, questions and coverage are inferred from the role title alone."
              }
            />
            <CardBody>
              <textarea
                className="field"
                rows={ctx.opportunity.jd_present ? 5 : 10}
                value={jd}
                onChange={(e) => setJd(e.target.value)}
                placeholder="Paste the full job posting here."
              />
              <div className="mt-3 flex items-center gap-3">
                <Button
                  loading={savingJd}
                  disabled={jd.trim().length < 40}
                  onClick={() => {
                    setSavingJd(true);
                    void attachJobDescription(id, jd.trim())
                      .then((next) => {
                        setCtx(next);
                        setJd("");
                        setMessage("Job description saved. Requirements and match updated.");
                      })
                      .catch((e) =>
                        setMessage(e instanceof Error ? e.message : "Could not save it"),
                      )
                      .finally(() => setSavingJd(false));
                  }}
                >
                  {ctx.opportunity.jd_present ? "Replace" : "Save job description"}
                </Button>
                <p className="text-xs text-[var(--color-text-muted)]">
                  Paste the whole posting — requirements are parsed from it.
                </p>
              </div>
              {message ? (
                <p className="mt-2 text-sm text-[var(--color-text-secondary)]">{message}</p>
              ) : null}
            </CardBody>
          </Card>

          {ctx.opportunity.jd_present ? (
            <div className="grid gap-4 lg:grid-cols-2">
              <List title="Must have" items={ctx.opportunity.must_have} />
              <List title="Nice to have" items={ctx.opportunity.nice_to_have} />
              <List title="Technologies" items={ctx.opportunity.technologies} />
              <List
                title="Likely themes"
                items={ctx.opportunity.likely_themes.map((x) => x.value)}
              />
            </div>
          ) : null}
        </div>
      ) : tab === "settings" ? (
        <Card>
          <CardHeader title="Workspace settings" />
          <CardBody>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="space-y-1 text-sm">
                <span>Interview date</span>
                <input
                  type="datetime-local"
                  className="field"
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                />
              </label>
              <label className="space-y-1 text-sm">
                <span>Integrity mode</span>
                <select
                  className="field"
                  value={integrity}
                  onChange={(e) => setIntegrity(e.target.value)}
                >
                  <option value="assisted">Assisted</option>
                  <option value="proctored">Proctored — live help disabled</option>
                </select>
              </label>
            </div>
            <div className="mt-4 flex gap-2">
              <Button
                onClick={() =>
                  void updateWorkspace(id, {
                    interview_at: date ? new Date(date).toISOString() : null,
                    integrity_mode: integrity,
                  }).then(() => setMessage("Settings saved."))
                }
              >
                Save settings
              </Button>
              <Button
                variant="ghost"
                onClick={() => void archiveWorkspace(id).then(() => router.push("/workspaces"))}
              >
                Archive
              </Button>
            </div>
            {message ? (
              <p className="mt-2 text-sm text-[var(--color-positive)]">{message}</p>
            ) : null}
          </CardBody>
        </Card>
      ) : tab === "sessions" ? (
        <Card>
          <CardHeader
            title="Session history"
            meta="Reports and active sessions for this opportunity."
          />
          <CardBody>
            <Button href={`/sessions?workspace=${id}`}>Open interview history</Button>
          </CardBody>
        </Card>
      ) : (
        launch
      )}
    </div>
  );
}
function List({ title, items }: { title: string; items: string[] }) {
  return (
    <Card>
      <CardHeader title={title} action={<Badge>{String(items.length)}</Badge>} />
      <CardBody>
        {items.length ? (
          <ul className="space-y-2 text-sm">
            {items.map((x, i) => (
              <li className="flex gap-2" key={i}>
                <span className="text-[var(--color-accent)]">•</span>
                {x}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-[var(--color-text-muted)]">Nothing identified yet.</p>
        )}
      </CardBody>
    </Card>
  );
}
