"use client";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, PageHeader } from "@/components/ui/card";
import { Empty } from "@/components/ui/states";
import { approveStory, createStory, generateStories, listStories } from "@/features/graph/api";
import type { Story } from "@/lib/api/types";
export default function StoriesPage() {
  const [stories, setStories] = useState<Story[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    title: "",
    situation: "",
    task: "",
    actions: "",
    result: "",
    skills: "",
  });
  const load = () => void listStories().then(setStories);
  useEffect(load, []);
  async function create() {
    setBusy(true);
    await createStory({
      title: form.title,
      categories: ["behavioral"],
      situation: form.situation,
      task: form.task,
      actions: form.actions.split("\n").filter(Boolean),
      result: form.result,
      skills_demonstrated: form.skills
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean),
    });
    setOpen(false);
    setForm({ title: "", situation: "", task: "", actions: "", result: "", skills: "" });
    load();
    setBusy(false);
  }
  return (
    <div className="space-y-7">
      <PageHeader
        title="Story bank"
        description="Reusable STAR stories grounded in your approved experience."
        action={
          <div className="flex gap-2">
            <Button variant="secondary" onClick={() => void generateStories().then(load)}>
              Generate suggestions
            </Button>
            <Button onClick={() => setOpen(!open)}>Add story</Button>
          </div>
        }
      />
      {open ? (
        <Card>
          <CardHeader title="New STAR story" />
          <CardBody>
            <div className="grid gap-3 sm:grid-cols-2">
              <input
                className="field sm:col-span-2"
                placeholder="Story title"
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
              />
              {(["situation", "task", "result"] as const).map((k) => (
                <textarea
                  key={k}
                  rows={3}
                  className="field"
                  placeholder={k.charAt(0).toUpperCase() + k.slice(1)}
                  value={form[k]}
                  onChange={(e) => setForm({ ...form, [k]: e.target.value })}
                />
              ))}
              <textarea
                rows={4}
                className="field"
                placeholder="Actions — one per line"
                value={form.actions}
                onChange={(e) => setForm({ ...form, actions: e.target.value })}
              />
              <input
                className="field sm:col-span-2"
                placeholder="Skills, comma separated"
                value={form.skills}
                onChange={(e) => setForm({ ...form, skills: e.target.value })}
              />
            </div>
            <div className="mt-3 flex justify-end">
              <Button loading={busy} disabled={!form.title} onClick={() => void create()}>
                Save suggestion
              </Button>
            </div>
          </CardBody>
        </Card>
      ) : null}
      {stories.length === 0 ? (
        <Empty
          title="No stories yet"
          description="Stories are reusable STAR answers built from experience you've already approved. Generate suggestions from your profile, or write one yourself."
          action={{ label: "Add your resume", href: "/documents" }}
        />
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        {stories.map((s) => (
          <Card key={s.id}>
            <CardHeader
              title={s.title}
              action={
                <Badge tone={s.status === "approved" ? "positive" : "warning"}>
                  {s.status}
                </Badge>
              }
            />
            <CardBody>
              <div className="space-y-3 text-sm">
                <p>
                  <strong>Situation:</strong> {s.situation || "Not added"}
                </p>
                <p>
                  <strong>Action:</strong> {s.actions.join(" ") || "Not added"}
                </p>
                <p>
                  <strong>Result:</strong> {s.result || "Not added"}
                </p>
                <div className="flex items-center justify-between">
                  <span className="text-xs text-[var(--color-text-muted)]">
                    ~{s.speak_time_seconds ?? 0}s
                  </span>
                  {s.status !== "approved" ? (
                    <Button size="sm" onClick={() => void approveStory(s.id).then(load)}>
                      Approve for interviews
                    </Button>
                  ) : null}
                </div>
              </div>
            </CardBody>
          </Card>
        ))}
      </div>
    </div>
  );
}
