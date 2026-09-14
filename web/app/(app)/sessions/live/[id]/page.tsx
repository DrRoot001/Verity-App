"use client";

/**
 * Live Copilot (PRD §14, §26).
 *
 * The microphone listens to the room. Every finished utterance goes to the
 * server as the interviewer's channel, and the server's detector decides what
 * counts as a question — the candidate types nothing and asks for nothing.
 * Typing stays available as a fallback for a question the room audio missed.
 */

import { use, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody } from "@/components/ui/card";
import { getLiveSession, issueLiveTicket } from "@/features/sessions/api";
import { GuidancePanel, type Answer } from "@/features/sessions/guidance";
import { LiveSidebar } from "@/features/sessions/live-sidebar";
import {
  openLiveSocket,
  sendEvent,
  sendHeard,
  type ServerFrame,
} from "@/features/sessions/socket";
import { InterimTranscript, MicButton } from "@/features/speech/mic-button";
import { useListening } from "@/features/speech/recognition";

export default function LiveHud({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();

  const socket = useRef<WebSocket | null>(null);
  const seq = useRef(0);

  const [status, setStatus] = useState("Connecting");
  const [connected, setConnected] = useState(false);
  const [question, setQuestion] = useState("");
  const [input, setInput] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [generating, setGenerating] = useState(false);
  const [latency, setLatency] = useState<Record<string, number>>({});
  const [unverified, setUnverified] = useState<string[]>([]);
  const [history, setHistory] = useState<Array<{ q: string; a?: string }>>([]);
  const [heard, setHeard] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Every heard utterance goes to the server; the server decides what is a
  // question. The client deliberately does no filtering of its own.
  const listening = useListening({
    onUtterance: ({ text, startMs, endMs }) => {
      setHeard((previous) => [...previous.slice(-40), text]);
      if (socket.current) sendHeard(socket.current, id, text, startMs, endMs);
    },
  });
  const { stop: stopListening } = listening;

  const handle = useCallback(
    (event: ServerFrame) => {
      seq.current = Math.max(seq.current, event.seq);

      switch (event.type) {
        case "session.ready":
          setStatus("Ready");
          break;
        case "question.finalized": {
          const content = String(event.payload.content ?? "");
          // The server also decides whether this is worth answering. Logistics
          // and small talk are finalized but not generated for, and promoting
          // them to "current question" makes the HUD look broken mid-interview.
          if (!event.payload.should_generate) break;
          setQuestion(content);
          setAnswer(null);
          setUnverified([]);
          setGenerating(true);
          setStatus("Question detected");
          setHistory((current) => [...current, { q: content }]);
          break;
        }
        case "answer.field_complete": {
          // The fast lane, ~1s ahead of the full answer. Show it immediately
          // rather than holding a spinner while the main lane finishes.
          if (event.payload.field !== "answer_direction") break;
          const direction = String(event.payload.value ?? "");
          if (direction) {
            setAnswer((current) => current ?? { answer_direction: direction });
            setStatus("Direction ready");
          }
          break;
        }
        case "answer.complete": {
          const content = event.payload.content as Answer;
          setAnswer(content);
          setGenerating(false);
          setLatency((event.payload.latency_ms ?? {}) as Record<string, number>);
          setUnverified(
            ((event.payload.grounding as { unverified?: string[] } | undefined)?.unverified ??
              []) as string[],
          );
          setStatus("Guidance ready");
          setHistory((current) =>
            current.map((item, index) =>
              index === current.length - 1
                ? { ...item, a: String(content.spoken_answer || content.answer_direction || "") }
                : item,
            ),
          );
          break;
        }
        case "warning":
          setError(String(event.payload.message ?? "Session warning"));
          break;
        case "session.ended":
          router.push(`/sessions/live/${id}/report`);
          break;
      }
    },
    [id, router],
  );

  useEffect(() => {
    let closed = false;

    void Promise.all([getLiveSession(id), issueLiveTicket(id)])
      .then(([session, { ticket, url }]) => {
        if (closed) return;
        const ws = openLiveSocket(url, ticket);
        socket.current = ws;

        ws.onopen = () => {
          setConnected(true);
          setStatus("Ready");
          sendEvent(ws, id, "session.start", {
            workspace_id: session.workspace_id,
            interview_type: session.interview_type,
            response_mode: session.response_mode,
            language: "en-US",
            client: { platform: "web" },
          });
        };
        ws.onmessage = (message) => handle(JSON.parse(String(message.data)) as ServerFrame);
        ws.onclose = () => {
          setConnected(false);
          setStatus("Disconnected");
        };
        ws.onerror = () => {
          setConnected(false);
          setStatus("Disconnected");
          setError("The realtime connection was interrupted.");
        };
      })
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : "Could not open the session"),
      );

    return () => {
      closed = true;
      socket.current?.close();
    };
  }, [id, handle]);

  function ask() {
    if (!input.trim() || !socket.current) return;
    const content = input.trim();
    setQuestion(content);
    setAnswer(null);
    setGenerating(true);
    setStatus("Generating");
    setHistory((current) => [...current, { q: content }]);
    sendEvent(socket.current, id, "question.manual", {
      content,
      response_mode: "balanced",
    });
    setInput("");
  }

  function end() {
    stopListening();
    if (socket.current) sendEvent(socket.current, id, "session.end", {});
  }

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span
            className={`size-2.5 rounded-full ${
              listening.listening
                ? "animate-pulse bg-[var(--color-live)]"
                : "bg-[var(--color-text-muted)]"
            }`}
          />
          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-[var(--color-live)]">
              Live copilot
            </p>
            <h1 className="text-lg">Interview session</h1>
          </div>
          <Badge tone={status === "Guidance ready" ? "positive" : "accent"}>{status}</Badge>
        </div>
        <div className="flex items-center gap-2">
          {listening.supported ? (
            <MicButton
              listening={listening.listening}
              onClick={listening.toggle}
              disabled={!connected}
              label={listening.listening ? "Listening to the room" : "Start listening"}
            />
          ) : null}
          <Button variant="danger" size="sm" onClick={end}>
            End session
          </Button>
        </div>
      </header>

      {!listening.supported ? (
        <div className="rounded-lg border border-[var(--color-warning-border)] bg-[var(--color-warning-quiet)] p-3 text-sm">
          This browser can&apos;t listen. Chrome can. You can still type the interviewer&apos;s
          questions below.
        </div>
      ) : null}
      {listening.listening ? (
        <div className="rounded-lg border border-[var(--color-border-subtle)] bg-[var(--color-raised)] p-3 text-xs text-[var(--color-text-secondary)]">
          Listening through your microphone — keep the call on speakers so it can hear the
          interviewer. Recognition is performed by your browser&apos;s speech service.
        </div>
      ) : null}
      {error ? (
        <div className="rounded-lg border border-[var(--color-warning-border)] bg-[var(--color-warning-quiet)] p-3 text-sm">
          {error}
        </div>
      ) : null}
      {listening.error ? (
        <div className="rounded-lg border border-[var(--color-warning-border)] bg-[var(--color-warning-quiet)] p-3 text-sm">
          {listening.error}
        </div>
      ) : null}

      <div className="grid min-h-[65vh] gap-4 lg:grid-cols-[minmax(0,1.65fr)_minmax(18rem,.75fr)]">
        <div className="space-y-4">
          <Card>
            <CardBody>
              <p className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)]">
                Current question
              </p>
              <p className="mt-3 min-h-16 text-xl leading-relaxed">
                {question ||
                  (listening.listening
                    ? "Listening. Questions are detected automatically."
                    : "Turn the microphone on, or type a question below.")}
              </p>
              <InterimTranscript text={listening.interim} />
            </CardBody>
          </Card>

          <Card>
            <CardBody>
              <p className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)]">
                Guidance
              </p>
              <GuidancePanel
                answer={answer}
                latency={latency}
                generating={generating}
                unverified={unverified}
              />
            </CardBody>
          </Card>

          <div className="rounded-xl border border-[var(--color-border-strong)] bg-[var(--color-surface)] p-3 shadow-[var(--shadow-2)]">
            <div className="flex gap-2">
              <input
                className="field"
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") ask();
                }}
                placeholder="Missed a question? Type it here."
              />
              <Button onClick={ask} disabled={!input.trim()}>
                Get guidance
              </Button>
            </div>
          </div>
        </div>

        <LiveSidebar history={history} heard={heard} />
      </div>
    </div>
  );
}
