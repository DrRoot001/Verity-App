"use client";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, PageHeader } from "@/components/ui/card";
import { Empty, Loading } from "@/components/ui/states";
import { approveNode, getProfile, listNodes, rejectNode } from "@/features/graph/api";
import type { GraphNode } from "@/lib/api/types";
const TYPES = ["experience", "skill", "achievement", "education"];
export default function ProfilePage() {
  const [profile, setProfile] = useState<Awaited<ReturnType<typeof getProfile>> | null>(null);
  const [type, setType] = useState("experience");
  const [nodes, setNodes] = useState<GraphNode[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = (t = type) => {
    setNodes(null);
    void Promise.all([getProfile(), listNodes(t)]).then(([p, n]) => {
      setProfile(p);
      setNodes(n);
    });
  };
  // Loading is intentionally keyed only by the selected entity type.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => load(type), [type]);
  async function act(n: GraphNode, a: "approve" | "reject") {
    setBusy(n.id);
    if (a === "approve") await approveNode(n.entity_type, n.id);
    else await rejectNode(n.entity_type, n.id);
    load();
    setBusy(null);
  }
  return (
    <div className="space-y-7">
      <PageHeader
        title="Candidate profile"
        description="Only facts you approve can be cited during an interview."
        action={<Button href="/documents">Add resume</Button>}
      />
      {profile ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <Card>
            <CardBody>
              <p className="text-2xl numeric">
                {Object.values(profile.approved_counts).reduce((a, b) => a + b, 0)}
              </p>
              <p className="text-xs text-[var(--color-text-muted)]">Approved facts</p>
            </CardBody>
          </Card>
          <Card>
            <CardBody>
              <p className="text-2xl numeric">
                {Object.values(profile.pending_counts).reduce((a, b) => a + b, 0)}
              </p>
              <p className="text-xs text-[var(--color-text-muted)]">Awaiting review</p>
            </CardBody>
          </Card>
          <Card>
            <CardBody>
              <p className="text-lg">{profile.target_role ?? "Not set"}</p>
              <p className="text-xs text-[var(--color-text-muted)]">Target role</p>
            </CardBody>
          </Card>
        </div>
      ) : null}
      <div className="flex gap-1 overflow-x-auto border-b border-[var(--color-border-subtle)]">
        {TYPES.map((t) => (
          <button
            key={t}
            onClick={() => setType(t)}
            className={`border-b-2 px-4 py-2 text-sm capitalize ${type === t ? "border-[var(--color-accent)] text-[var(--color-accent)]" : "border-transparent text-[var(--color-text-secondary)]"}`}
          >
            {t}
          </button>
        ))}
      </div>
      {!nodes ? (
        <Loading />
      ) : nodes.length === 0 ? (
        <Empty
          title={`No ${type} yet`}
          description="Upload or paste a resume and Verity will extract items for your review."
          action={{ label: "Add resume", href: "/documents" }}
        />
      ) : (
        <div className="grid gap-3 lg:grid-cols-2">
          {nodes.map((n) => (
            <Card key={n.id}>
              <CardHeader
                title={n.label}
                action={
                  <Badge
                    tone={
                      n.provenance.status === "approved"
                        ? "positive"
                        : n.provenance.status === "pending_review"
                          ? "warning"
                          : "neutral"
                    }
                  >
                    {n.provenance.status.replaceAll("_", " ")}
                  </Badge>
                }
              />
              <CardBody>
                <dl className="space-y-1 text-sm">
                  {Object.entries(n.detail)
                    .filter(([, v]) => v != null && String(v) !== "")
                    .slice(0, 5)
                    .map(([k, v]) => (
                      <div className="flex gap-3" key={k}>
                        <dt className="w-28 shrink-0 capitalize text-[var(--color-text-muted)]">
                          {k.replaceAll("_", " ")}
                        </dt>
                        <dd className="min-w-0 break-words">
                          {Array.isArray(v) ? v.join(", ") : String(v)}
                        </dd>
                      </div>
                    ))}
                </dl>
                {n.provenance.status === "pending_review" ? (
                  <div className="mt-4 flex gap-2">
                    <Button
                      size="sm"
                      loading={busy === n.id}
                      onClick={() => void act(n, "approve")}
                    >
                      Approve
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => void act(n, "reject")}>
                      Reject
                    </Button>
                  </div>
                ) : null}
              </CardBody>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
