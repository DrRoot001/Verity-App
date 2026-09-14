"use client";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, PageHeader } from "@/components/ui/card";
import { listResumeVersions, pasteResume, uploadResume } from "@/features/graph/api";
import type { IngestionResult } from "@/lib/api/types";
export default function DocumentsPage() {
  const [versions, setVersions] = useState<IngestionResult["version"][]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const load = () => void listResumeVersions().then(setVersions);
  useEffect(load, []);
  async function paste() {
    setBusy(true);
    try {
      const r = await pasteResume(text);
      setMessage(
        r.experiences_found + r.skills_found > 0
          ? `Extracted ${r.experiences_found} experiences and ${r.skills_found} skills. Review them before interviews.`
          : "We couldn't find any experience or skills in that text. Check it includes " +
              "your work history with dates.",
      );
      setText("");
      load();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Ingestion failed");
    } finally {
      setBusy(false);
    }
  }
  async function upload(file: File) {
    setBusy(true);
    try {
      const r = await uploadResume(file);
      // Zero facts from a file that uploaded fine is a failure, not a success.
      // Reporting it as "uploaded successfully" is how the earlier extraction
      // bug stayed invisible: the file was read, and nothing came out of it.
      setMessage(
        r.created_node_count > 0
          ? `Uploaded. ${r.created_node_count} facts await your review.`
          : "We read the file but couldn't find any experience or skills in it. " +
              "Paste the text below instead — that path is more reliable for unusual layouts.",
      );
      load();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-8">
      <PageHeader
        title="Documents"
        description="Add a resume once; approved facts become grounded evidence in every workspace."
      />
      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader title="Upload resume" meta="PDF, DOCX or text · maximum 10 MB" />
          <CardBody>
            <label className="flex min-h-44 cursor-pointer flex-col items-center justify-center rounded-xl border border-dashed border-[var(--color-border-strong)] bg-[var(--color-raised)] p-6 text-center">
              <span className="text-2xl">⇧</span>
              <span className="mt-2 font-medium">Choose a resume</span>
              <span className="text-sm text-[var(--color-text-muted)]">
                Extraction starts immediately
              </span>
              <input
                className="sr-only"
                type="file"
                accept=".pdf,.docx,.txt"
                disabled={busy}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) void upload(f);
                }}
              />
            </label>
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Paste resume text" meta="Use this when a file cannot be parsed." />
          <CardBody>
            <textarea
              rows={7}
              className="field"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Paste at least 100 characters…"
            />
            <div className="mt-3 flex justify-end">
              <Button
                loading={busy}
                disabled={text.trim().length < 100}
                onClick={() => void paste()}
              >
                Extract profile
              </Button>
            </div>
          </CardBody>
        </Card>
      </div>
      {message ? (
        <p role="status" className="rounded-lg bg-[var(--color-info-quiet)] p-3 text-sm">
          {message}
        </p>
      ) : null}
      <section className="space-y-3">
        <h2 className="text-lg">Resume versions</h2>
        {versions.length ? (
          <div className="grid gap-3 md:grid-cols-2">
            {versions.map((v) => (
              <Card key={v.id}>
                <CardBody>
                  <div className="flex justify-between">
                    <div>
                      <p className="font-medium">Resume version {v.version}</p>
                      <p className="text-xs text-[var(--color-text-muted)]">
                        {v.parsed_at ? new Date(v.parsed_at).toLocaleString() : "Processing"}
                      </p>
                    </div>
                    <Badge tone={v.status === "processed" ? "positive" : "neutral"}>
                      {v.status}
                    </Badge>
                  </div>
                </CardBody>
              </Card>
            ))}
          </div>
        ) : (
          <p className="text-sm text-[var(--color-text-muted)]">No resumes uploaded yet.</p>
        )}
      </section>
    </div>
  );
}
