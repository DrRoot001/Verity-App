"use client";

/**
 * Mock interview room (PRD §11).
 *
 * The AI is the interviewer: it asks, probes a thin answer, and adapts
 * difficulty. The candidate answers out loud and never touches a control —
 * the microphone opens when the question finishes and the answer submits after
 * a natural pause. Practising the thing being practised requires that the
 * interface get out of the way.
 *
 * Feedback stays hidden until the report. Coaching mid-interview trains the
 * candidate to expect a safety net a real interview will not give them.
 */

import { use, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody } from "@/components/ui/card";
import { Loading } from "@/components/ui/states";
import {
  answerMock,
  endMock,
  getMockSession,
  nextMockTurn,
  type MockSession,
  type MockTurn,
} from "@/features/sessions/api";
import { useHandsFree } from "@/features/sessions/hands-free";
import { InterimTranscript, MicButton } from "@/features/speech/mic-button";
import { useListening } from "@/features/speech/recognition";
import { useSpeaker } from "@/features/speech/voice";

const INTENT_LABEL: Record<string, string> = {
  question: "New question",
  probe: "Follow-up",
  redirect: "Redirect",
  wrapup: "Wrapping up",
};

type Phase = "asking" | "listening" | "thinking";

export default function MockRoom({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();

  const [session, setSession] = useState<MockSession | null>(null);
  const [turn, setTurn] = useState<MockTurn | null>(null);
  const [answer, setAnswer] = useState("");
  const [phase, setPhase] = useState<Phase>("asking");
  const [error, setError] = useState<string | null>(null);
  const started = useRef(Date.now());
  // Read inside callbacks that must not be re-created on every keystroke.
  const answerRef = useRef("");
  answerRef.current = answer;
  const busy = useRef(false);

  const speaker = useSpeaker();
  const listeningRef = useRef<{ start: () => void; stop: () => void } | null>(null);

  const finish = useCallback(async () => {
    busy.current = true;
    listeningRef.current?.stop();
    speaker.cancel();
    try {
      await endMock(id);
      router.push(`/sessions/mock/${id}/report`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not finish");
      busy.current = false;
    }
  }, [id, router, speaker]);

  /** Speak a turn, then hand the floor to the candidate. */
  const present = useCallback(
    (next: MockTurn) => {
      setTurn(next);
      setAnswer("");
      started.current = Date.now();
      setPhase("asking");
      speaker.speak(next.utterance, () => {
        if (!next.expects_answer) return;
        setPhase("listening");
        listeningRef.current?.start();
      });
    },
    [speaker],
  );

  const submit = useCallback(async () => {
    const spoken = answerRef.current.trim();
    if (!spoken || busy.current) return;

    busy.current = true;
    listeningRef.current?.stop();
    setPhase("thinking");
    try {
      const result = await answerMock(
        id,
        spoken,
        Math.max(1, Math.round((Date.now() - started.current) / 1000)),
      );
      if (!result.next_turn.expects_answer) {
        speaker.speak(result.next_turn.utterance);
        await finish();
        return;
      }
      present(result.next_turn);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not submit answer");
      setPhase("listening");
    } finally {
      busy.current = false;
    }
  }, [id, present, speaker, finish]);

  const handsFree = useHandsFree(() => void submit());
  const { noteSpeech, clear: clearCountdown, enabled: autoMode } = handsFree;

  const listening = useListening({
    onUtterance: ({ text }) => {
      setAnswer((current) => `${current} ${text}`.trim());
      if (autoMode) noteSpeech();
    },
  });
  listeningRef.current = { start: listening.start, stop: listening.stop };

  useEffect(() => {
    void getMockSession(id)
      .then(async (loaded) => {
        setSession(loaded);
        if (loaded.status === "completed") {
          router.replace(`/sessions/mock/${id}/report`);
          return;
        }
        present(await nextMockTurn(id));
      })
      .catch((caught) =>
        setError(caught instanceof Error ? caught.message : "Could not open session"),
      );
    // Re-running would restart the interview from its first question.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, router]);

  if (!session || !turn) return <Loading rows={4} label="Preparing your interviewer" />;

  const words = answer.trim() ? answer.trim().split(/\s+/).length : 0;
  const status =
    phase === "thinking"
      ? "Thinking…"
      : phase === "asking"
        ? speaker.speaking
          ? "Interviewer speaking"
          : "Interviewer"
        : handsFree.countdown !== null
          ? `Your turn — submitting in ${handsFree.countdown}s`
          : "Your turn";

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-[var(--color-accent)]">
              Mock interview
            </p>
            <h1 className="mt-0.5 text-xl capitalize">{session.mode.replaceAll("_", " ")}</h1>
          </div>
          <Badge tone={phase === "listening" ? "positive" : "accent"}>{status}</Badge>
        </div>
        <div className="flex items-center gap-2">
          {speaker.supported ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => speaker.setMuted(!speaker.muted)}
              aria-pressed={speaker.muted}
            >
              {speaker.muted ? "Voice off" : "Voice on"}
            </Button>
          ) : null}
          <Button variant="ghost" size="sm" onClick={() => void finish()}>
            End &amp; report
          </Button>
        </div>
      </header>

      <Card>
        <CardBody>
          <div className="min-h-40 space-y-4">
            <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-text-muted)]">
              <Badge tone={turn.intent === "probe" ? "warning" : "accent"}>
                {INTENT_LABEL[turn.intent] ?? turn.intent}
              </Badge>
              <span>Question {Math.max(1, turn.sequence)}</span>
              <span>·</span>
              <span>Difficulty {turn.difficulty}/5</span>
            </div>
            <p className="max-w-3xl text-xl leading-relaxed">{turn.utterance}</p>
            {speaker.supported && !speaker.muted ? (
              <Button variant="ghost" size="sm" onClick={() => speaker.speak(turn.utterance)}>
                Repeat question
              </Button>
            ) : null}
          </div>
        </CardBody>
      </Card>

      <div className="rounded-[var(--radius-card)] border border-[var(--color-border-strong)] bg-[var(--color-surface)] p-4 shadow-[var(--shadow-2)]">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <label htmlFor="answer" className="text-sm font-medium">
            Your answer
          </label>
          <div className="flex items-center gap-3">
            {listening.supported ? (
              <label className="flex items-center gap-2 text-xs text-[var(--color-text-secondary)]">
                <input
                  type="checkbox"
                  checked={handsFree.enabled}
                  onChange={(event) => handsFree.setEnabled(event.target.checked)}
                />
                Hands-free
              </label>
            ) : null}
            {listening.supported ? (
              <MicButton
                listening={listening.listening}
                onClick={() => {
                  clearCountdown();
                  listening.toggle();
                }}
                label={listening.listening ? "Listening" : "Answer by voice"}
              />
            ) : null}
          </div>
        </div>

        <textarea
          id="answer"
          rows={6}
          className="field mt-2"
          value={answer}
          onChange={(event) => {
            setAnswer(event.target.value);
            clearCountdown();
          }}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") void submit();
          }}
          placeholder={
            listening.supported
              ? "Speak your answer — this fills in as you talk."
              : "Answer naturally. Use specific actions and outcomes."
          }
        />
        <InterimTranscript text={listening.interim} />

        {!listening.supported ? (
          <p className="mt-2 text-xs text-[var(--color-text-muted)]">
            Voice answers need Chrome. You can still type.
          </p>
        ) : null}
        {listening.error ? (
          <p className="mt-2 text-sm text-[var(--color-warning)]">{listening.error}</p>
        ) : null}

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <p className="numeric text-xs text-[var(--color-text-muted)]">
            {words} words ·{" "}
            {handsFree.enabled && listening.supported
              ? "submits automatically when you stop speaking"
              : "⌘ Enter to submit"}{" "}
            · feedback stays hidden until the report
          </p>
          <Button
            onClick={() => void submit()}
            loading={phase === "thinking"}
            disabled={!answer.trim()}
          >
            Submit answer
          </Button>
        </div>
        {error ? <p className="mt-2 text-sm text-[var(--color-critical)]">{error}</p> : null}
      </div>
    </div>
  );
}
