"use client";

/** Session diagnostics pane (PRD §18.3). Metadata only — the on-call view. */

import { useState } from "react";

import { Stat } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { sessionDiagnostics, type SessionDiagnostics } from "@/features/admin/api";
import { ApiError } from "@/lib/api/client";

export function DiagnosticsPane() {
  const [sessionId, setSessionId] = useState("");
  const [data, setData] = useState<SessionDiagnostics | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function load() {
    setData(null);
    try {
      setData(await sessionDiagnostics(sessionId.trim()));
      setMessage(null);
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Lookup failed");
    }
  }

  return (
    <Card>
      <CardHeader
        title="Session diagnostics"
        meta="Per-stage latency and grounding outcomes. Metadata only — no transcript."
      />
      <CardBody>
        <div className="flex gap-2 pb-4">
          <input
            className="field flex-1 font-mono text-xs"
            placeholder="Live session id"
            value={sessionId}
            onChange={(event) => setSessionId(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void load();
            }}
          />
          <Button size="sm" onClick={() => void load()}>
            Look up
          </Button>
        </div>

        {message ? (
          <p role="status" className="text-sm text-[var(--color-text-secondary)]">
            {message}
          </p>
        ) : null}

        {data ? (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
              <Stat value={data.status} label="Status" />
              <Stat value={String(data.generations)} label="Generations" />
              <Stat value={`${data.metered_seconds}s`} label="Metered" />
              <Stat value={String(data.last_event_seq)} label="Last seq" />
            </div>

            <p className="text-sm text-[var(--color-text-secondary)]">
              Grounding: {data.grounding.checked} claim(s) checked,{" "}
              <span
                className={data.grounding.downgraded > 0 ? "text-[var(--color-warning)]" : ""}
              >
                {data.grounding.downgraded} downgraded
              </span>
              .
            </p>

            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-left text-[var(--color-text-secondary)]">
                  <tr>
                    <th className="pb-1 font-medium">Suggestion</th>
                    <th className="pb-1 text-right font-medium">rev</th>
                    <th className="pb-1 text-right font-medium">retrieval</th>
                    <th className="pb-1 text-right font-medium">generation</th>
                    <th className="pb-1 text-right font-medium">e2e</th>
                  </tr>
                </thead>
                <tbody className="numeric">
                  {data.latencies.map((row) => (
                    <tr
                      key={String(row.suggestion_id)}
                      className="border-t border-[var(--color-border-subtle)]"
                    >
                      <td className="py-1 pr-3 font-mono">
                        {String(row.suggestion_id).slice(0, 8)}
                      </td>
                      <td className="py-1 text-right">{String(row.revision ?? "")}</td>
                      <td className="py-1 text-right">{fmt(row.retrieval)}</td>
                      <td className="py-1 text-right">{fmt(row.generation)}</td>
                      <td className="py-1 text-right">{fmt(row.e2e)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

function fmt(value: unknown): string {
  return typeof value === "number" ? `${Math.round(value)}ms` : "—";
}
