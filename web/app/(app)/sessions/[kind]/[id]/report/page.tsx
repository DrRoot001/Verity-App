"use client";
import { use, useEffect, useState } from "react";
import { Badge, Stat } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, PageHeader } from "@/components/ui/card";
import { Loading } from "@/components/ui/states";
import { getLiveReport, getMockReport, type SessionReport } from "@/features/sessions/api";
export default function ReportPage({
  params,
}: {
  params: Promise<{ kind: string; id: string }>;
}) {
  const { kind, id } = use(params);
  const [report, setReport] = useState<SessionReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void (kind === "live" ? getLiveReport(id) : getMockReport(id))
      .then(setReport)
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load report"));
  }, [id, kind]);
  if (error)
    return (
      <p role="alert" className="text-[var(--color-critical)]">
        {error}
      </p>
    );
  if (!report) return <Loading rows={5} label="Building report" />;
  const entries = Object.entries(report.dimension_scores ?? {});
  return (
    <div className="space-y-8">
      <PageHeader
        title={kind === "live" ? "Interview coverage" : "Mock interview report"}
        description={report.summary ?? "Your session report is ready."}
        action={
          <Button href="/sessions" variant="secondary">
            All interviews
          </Button>
        }
      >
        <Badge tone="positive">{report.status}</Badge>
      </PageHeader>
      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardBody>
            <Stat
              value={
                report.overall_score == null
                  ? "Coverage"
                  : `${Math.round(report.overall_score)}%`
              }
              label={kind === "live" ? "Evidence-based review" : "AI assessment"}
              tone="accent"
            />
          </CardBody>
        </Card>
        {entries.slice(0, 2).map(([k, v]) => (
          <Card key={k}>
            <CardBody>
              <Stat value={`${Math.round(Number(v))}%`} label={k.replaceAll("_", " ")} />
            </CardBody>
          </Card>
        ))}
      </div>
      {entries.length ? (
        <Card>
          <CardHeader
            title="Dimensions"
            meta="Each score is an AI assessment, not an objective hiring outcome."
          />
          <CardBody>
            <div className="space-y-4">
              {entries.map(([k, v]) => (
                <div key={k}>
                  <div className="mb-1 flex justify-between text-sm">
                    <span className="capitalize">{k.replaceAll("_", " ")}</span>
                    <span className="numeric">{Math.round(Number(v))}</span>
                  </div>
                  <div className="h-2 rounded-full bg-[var(--color-raised)]">
                    <div
                      className="h-full rounded-full bg-[var(--color-accent)]"
                      style={{ width: `${Math.max(0, Math.min(100, Number(v)))}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </CardBody>
        </Card>
      ) : null}
      <div className="grid gap-4 lg:grid-cols-2">
        <List
          title="Strengths"
          items={report.strengths}
          empty="No strengths were measured yet."
        />
        <List
          title="Next improvements"
          items={[...report.weaknesses, ...report.recommendations]}
          empty="No recommendations were produced."
        />
      </div>
    </div>
  );
}
function List({ title, items, empty }: { title: string; items: unknown[]; empty: string }) {
  return (
    <Card>
      <CardHeader title={title} />
      <CardBody>
        {items.length ? (
          <ul className="space-y-3">
            {items.map((x, i) => (
              <li className="rounded-lg bg-[var(--color-raised)] p-3 text-sm" key={i}>
                {typeof x === "string"
                  ? x
                  : String(
                      (x as Record<string, unknown>).theme ??
                        (x as Record<string, unknown>).detail ??
                        JSON.stringify(x),
                    )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-[var(--color-text-muted)]">{empty}</p>
        )}
      </CardBody>
    </Card>
  );
}
